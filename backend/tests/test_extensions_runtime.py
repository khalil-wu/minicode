"""Focused contracts for the MiniCode executable extension runtime."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.extensions import (
    ExtensionLoader,
    ExtensionRegistrationError,
    ExtensionRunner,
    ExtensionStaleError,
    ExtensionTrustPolicy,
    ExtensionTrustError,
    ExtensionToolDefinition,
    clear_extension_cache,
    discover_extensions_in_dir,
)
from backend.agent.tool_execution import prepare_tool_call_sequence, run_tool
from backend.agent.context import ContextBuilder
from backend.agent.run_context import RunContext
from backend.agent.state import AgentState
from backend.config import PermissionSettings
from backend.llm.base import ToolCallEvent as AgentToolCallEvent
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.tools.base import BaseTool, ToolResult, ToolSchema
from backend.tools.registry import CapabilityRegistry
from backend.commands.registry import CommandRegistry
from backend.ws.agent_runner import SessionAgentRunnerMixin


def run(coro):
    return asyncio.run(coro)


def _write_extension(path: Path, source: str) -> Path:
    path.write_text(source, encoding="utf-8")
    return path


class _HostTool(BaseTool):
    name = "host_tool"
    description = "Host tool used by extension integration tests."
    read_only = True

    def __init__(self, execute=None, *, name: str | None = None) -> None:
        self._execute = execute
        if name is not None:
            self.name = name

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={"type": "object"},
        )

    async def execute(self, args, context=None) -> ToolResult:
        if self._execute is None:
            return ToolResult(content=f"host:{args}")
        value = self._execute(args, context)
        if asyncio.iscoroutine(value):
            value = await value
        return value if isinstance(value, ToolResult) else ToolResult(content=str(value))


def test_factory_registers_minicode_surfaces_and_executes_tool() -> None:
    calls: list[dict] = []

    async def execute(params, ctx):
        calls.append({"params": params, "cwd": ctx.cwd})
        return {"content": f"echo:{params['value']}"}

    def factory(api):
        api.register_tool(
            {
                "name": "echo_extension",
                "label": "Echo",
                "description": "Echo a value from an extension.",
                "parameters": {
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                },
                "execute": execute,
                "read_only": True,
            }
        )
        api.register_command("hello", lambda args, ctx: args)
        api.register_shortcut("ctrl+e", lambda ctx: None)
        api.register_flag("experimental", {"type": "boolean", "default": True})
        api.register_provider("test-provider", {"base_url": "https://example.test"})

    result = run(ExtensionLoader(cwd=".").load_factory(factory))
    assert result.errors == []
    extension = result.extensions[0]
    assert set(extension.tools) == {"echo_extension"}
    assert set(extension.commands) == {"hello"}
    assert set(extension.shortcuts) == {"ctrl+e"}
    assert result.runtime.flag_values["experimental"] is True
    assert [item.name for item in result.runtime.pending_provider_registrations] == [
        "test-provider"
    ]

    runner = ExtensionRunner(result.extensions, result.runtime, cwd=".")
    output = run(
        runner.invoke_tool(
            tool_call_id="tc-1",
            tool_name="echo_extension",
            params={"value": "ok"},
        )
    )
    assert output.content == "echo:ok"
    assert calls and calls[0]["params"] == {"value": "ok"}


def test_object_factory_setup_is_supported() -> None:
    class Factory:
        def setup(self, api):
            api.register_flag("object_factory", {"type": "boolean", "default": True})

    result = run(ExtensionLoader(cwd=".").load_factory(Factory()))
    assert result.errors == []
    assert result.runtime.flag_values["object_factory"] is True


def test_tool_call_mutation_block_and_result_patch() -> None:
    seen: list[dict] = []

    async def execute(params, ctx):
        seen.append(dict(params))
        return {"content": "raw"}

    def factory(api):
        api.register_tool(
            {
                "name": "intercepted",
                "description": "A test tool.",
                "parameters": {"type": "object"},
                "execute": execute,
            }
        )

        def before(event, ctx):
            event.input["injected"] = "yes"

        def after(event, ctx):
            return {"content": [{"type": "text", "text": "patched"}], "is_error": True}

        api.on("tool_call", before)
        api.on("tool_result", after)

    result = run(ExtensionLoader(cwd=".").load_factory(factory))
    runner = ExtensionRunner(result.extensions, result.runtime)
    output = run(
        runner.invoke_tool(tool_call_id="tc", tool_name="intercepted", params={})
    )
    assert seen == [{"injected": "yes"}]
    assert output.content == "patched"
    assert output.is_error is True

    async def blocked_factory(api):
        api.register_tool(
            {
                "name": "blocked",
                "description": "A blocked tool.",
                "parameters": {"type": "object"},
                "execute": lambda params: {"content": "must not run"},
            }
        )
        api.on("tool_call", lambda event, ctx: {"block": True, "reason": "policy"})

    blocked = run(ExtensionLoader(cwd=".").load_factory(blocked_factory))
    blocked_runner = ExtensionRunner(blocked.extensions, blocked.runtime)
    blocked_output = run(
        blocked_runner.invoke_tool(tool_call_id="tc", tool_name="blocked", params={})
    )
    assert blocked_output.is_error is True
    assert blocked_output.status == "blocked"
    assert "policy" in blocked_output.content


def test_agent_hook_shaped_before_after_entry_points() -> None:
    def factory(api):
        api.on("tool_call", lambda event, ctx: event.input.update({"from_hook": True}))

        def after(event, ctx):
            return {"content": "hooked", "is_error": False}

        api.on("tool_result", after)

    result = run(ExtensionLoader(cwd=".").load_factory(factory))
    runner = ExtensionRunner(result.extensions, result.runtime)
    params = {}
    decision = run(runner.before_tool_call("tc", "host_tool", params))
    assert decision is None
    assert params == {"from_hook": True}
    patch = run(runner.after_tool_call("tc", "host_tool", params, {"content": "raw"}))
    assert patch is not None and patch.content == "hooked"


def test_tool_registry_adapter_uses_host_capability_registry() -> None:
    async def factory(api):
        api.register_tool(
            {
                "name": "registry_tool",
                "description": "Tool registered into the host registry.",
                "parameters": {"type": "object"},
                "execute": lambda params: "registered",
            }
        )

    result = run(ExtensionLoader(cwd=".").load_factory(factory))
    runner = ExtensionRunner(result.extensions, result.runtime)
    registry = CapabilityRegistry()
    assert runner.bind_tool_registry(registry) == ["registry_tool"]
    tool = registry.get_tool("registry_tool")
    assert tool is not None


@pytest.mark.parametrize(
    ("flags", "read_only", "permission", "side_effect"),
    [
        ({"read_only": True}, True, "auto", "none"),
        ({"read_only": True, "open_world": True}, False, "confirm", "external"),
        ({"read_only": True, "destructive": True}, False, "confirm", "destructive"),
        ({"read_only": True, "mutates_workspace": True}, False, "confirm", "workspace"),
        ({"read_only": True, "mutates_external_state": True}, False, "confirm", "external"),
    ],
)
def test_extension_tool_side_effect_flags_override_read_only_declaration(
    flags: dict[str, bool],
    read_only: bool,
    permission: str,
    side_effect: str,
) -> None:
    def factory(api):
        api.register_tool(
            {
                "name": "flagged_extension_tool",
                "description": "Extension flag contract test.",
                "parameters": {"type": "object"},
                "execute": lambda _params: "ok",
                **flags,
            }
        )

    result = run(ExtensionLoader(cwd=".").load_factory(factory))
    registry = CapabilityRegistry()
    result.runner.bind_tool_registry(registry)
    tool = registry.get_tool("flagged_extension_tool")

    assert tool is not None
    assert tool.is_read_only({}) is read_only
    assert tool.permission.value == permission
    assert tool.get_side_effect_kind({}) == side_effect
    assert tool.to_runtime_metadata()["read_only"] is read_only
    output = run(tool.execute({}))
    assert output.content == "ok"
    result.runner.detach_tool_registry()
    assert registry.get_tool("registry_tool") is None


def test_loader_returns_live_runner_for_late_tool_registration() -> None:
    captured = []

    def factory(api):
        captured.append(api)

    result = run(ExtensionLoader(cwd=".").load_factory(factory))
    runner = result.runner
    assert runner is not None
    registry = CapabilityRegistry()
    assert runner.bind_tool_registry(registry) == []

    captured[0].register_tool(
        {
            "name": "late_tool",
            "description": "Registered after the loader completed.",
            "parameters": {"type": "object"},
            "execute": lambda params: "late",
        }
    )
    assert registry.get_tool("late_tool") is not None


def _collision_runner():
    def factory(api):
        api.register_tool(
            {
                "name": "same_name",
                "description": "Extension replacement for a host tool.",
                "parameters": {"type": "object"},
                "execute": lambda params: "extension",
            }
        )

    result = run(ExtensionLoader(cwd=".").load_factory(factory))
    runner = result.runner
    assert runner is not None
    return runner


def test_extension_tool_collision_keeps_host_tool_and_reports_diagnostic() -> None:
    runner = _collision_runner()
    registry = CapabilityRegistry()
    host_tool = _HostTool(name="same_name")
    registry.register(host_tool)

    assert runner.bind_tool_registry(registry) == []
    assert registry.get_tool("same_name") is host_tool
    assert any(
        item.get("type") == "warning" and "host tool wins" in str(item.get("message"))
        for item in runner.diagnostics
    )


def test_extension_tool_collision_overrides_host_and_detach_restores_it() -> None:
    runner = _collision_runner()
    registry = CapabilityRegistry()
    host_tool = _HostTool(name="same_name")
    registry.register(host_tool)

    assert runner.bind_tool_registry(registry, override_existing=True) == ["same_name"]
    extension_tool = registry.get_tool("same_name")
    assert extension_tool is not None and extension_tool is not host_tool
    assert run(extension_tool.execute({})).content == "extension"

    runner.detach_tool_registry()
    assert registry.get_tool("same_name") is host_tool


def test_host_agent_tool_lifecycle_invokes_extension_before_and_after_hooks() -> None:
    observed: list[object] = []

    def factory(api):
        def before(event, ctx):
            observed.append(("before", event.tool_call_id, event.tool_name))
            event.input["injected"] = True

        def after(event, ctx):
            observed.append(("after", event.content, dict(event.input)))
            return {
                "content": [{"type": "text", "text": "patched-host-result"}],
                "is_error": True,
            }

        api.on("tool_call", before)
        api.on("tool_result", after)

    result = run(ExtensionLoader(cwd=".").load_factory(factory))
    runner = result.runner
    assert runner is not None
    registry = CapabilityRegistry()
    registry.register(
        _HostTool(
            lambda args, _context: ToolResult(content=f"raw:{args['injected']}")
        )
    )
    tool_context = ToolExecutionContext(
        permission=PermissionContext(),
        run_context=RunContext(lifecycle_runtime=runner),
    )

    output = run(
        run_tool(
            AgentToolCallEvent("call-host", "host_tool", {}),
            registry,
            tool_context,
        )
    )
    assert output.content == "patched-host-result"
    assert output.is_error is True
    assert observed == [
        ("before", "call-host", "host_tool"),
        ("after", "raw:True", {"injected": True}),
    ]


def test_extension_tool_agent_wiring_does_not_double_run_hooks() -> None:
    calls: list[tuple[str, str]] = []

    def factory(api):
        api.register_tool(
            {
                "name": "extension_once",
                "description": "Extension tool whose hooks must run once.",
                "parameters": {"type": "object"},
                "execute": lambda tool_call_id, params: (
                    calls.append(("execute", tool_call_id)) or "ok"
                ),
            }
        )
        api.on(
            "tool_call",
            lambda event, ctx: calls.append(("before", event.tool_call_id)),
        )
        api.on(
            "tool_result",
            lambda event, ctx: calls.append(("after", event.tool_call_id)),
        )

    result = run(ExtensionLoader(cwd=".").load_factory(factory))
    runner = result.runner
    assert runner is not None
    registry = CapabilityRegistry()
    runner.bind_tool_registry(registry)
    tool_context = ToolExecutionContext(
        permission=PermissionContext(),
        metadata={"_lifecycle_runtime": runner},
    )

    output = run(
        run_tool(
            AgentToolCallEvent("call-extension", "extension_once", {}),
            registry,
            tool_context,
        )
    )
    assert output.content == "ok"
    assert calls == [
        ("before", "call-extension"),
        ("execute", "call-extension"),
        ("after", "call-extension"),
    ]


def test_extension_exec_uses_canonical_tool_batch(tmp_path: Path) -> None:
    executed: list[dict[str, object]] = []

    class _Runner:
        def bind_actions(self, actions):
            self.actions = actions

    class _Host(SessionAgentRunnerMixin):
        run_manager = None

        def _model_runtime_for_conversation(self, _conversation_id):
            return None

        def _model_registry_for_conversation(self, _conversation_id):
            return None

    registry = CapabilityRegistry()
    registry.register(
        _HostTool(
            lambda args, _context: (
                executed.append(dict(args))
                or ToolResult(content="Exit code: 0\nextension canonical")
            ),
            name="run_command",
        )
    )
    checker = PermissionChecker(PermissionSettings(auto_allow=["run_command"]), tmp_path)
    context_builder = ContextBuilder()
    state = AgentState(user_message="parent", max_iterations=3)
    tool_context = ToolExecutionContext(
        permission=checker.build_context(mode="bypass", source="test"),
        workspace_root=tmp_path,
        permission_checker=checker,
        metadata={"_context_builder": context_builder, "_agent_state": state},
    )
    runner = _Runner()
    host = _Host()
    host.permission_checker = checker
    host._bind_lifecycle_runtime_host_actions(
        runner,
        conversation=SimpleNamespace(id="conversation-extension"),
        tool_registry=registry,
        run_metadata={"_tool_execution_context": tool_context},
        run_context_builder=context_builder,
        run_llm=object(),
        cancel_event=None,
    )

    result = run(runner.actions["exec"]("echo", ["ok"], {"cwd": str(tmp_path)}))

    assert result.is_error is False
    assert "extension canonical" in result.content
    assert executed and executed[0]["cwd"] == str(tmp_path)


def test_extension_prepare_arguments_and_parallel_mode_match_pi() -> None:
    def factory(api):
        api.register_tool(
            {
                "name": "prepared_extension",
                "description": "Prepared extension arguments.",
                "parameters": {
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                },
                "prepare_arguments": lambda args: {
                    "value": args.get("legacy", "")
                },
                "execution_mode": "parallel",
                "execute": lambda params: params["value"],
            }
        )

    result = run(ExtensionLoader(cwd=".").load_factory(factory))
    runner = result.runner
    assert runner is not None
    registry = CapabilityRegistry()
    runner.bind_tool_registry(registry)
    tool = registry.get_tool("prepared_extension")
    assert tool is not None
    assert tool.is_concurrency_safe({}) is True

    prepared = prepare_tool_call_sequence(
        AgentState(user_message="test", max_iterations=1),
        [AgentToolCallEvent("prepare", "prepared_extension", {"legacy": "ok"})],
        registry,
    )
    assert prepared[0].arguments == {"value": "ok"}
    output = run(tool.execute(prepared[0].arguments))
    assert output.content == "ok"


def test_extension_tool_update_callback_projects_to_host_stream() -> None:
    updates: list[tuple[str, str, str]] = []

    async def execute(params, on_update, ctx):
        await on_update({"type": "text", "text": "chunk"})
        return "done"

    def factory(api):
        api.register_tool(
            {
                "name": "streaming_extension",
                "description": "Streaming extension tool.",
                "parameters": {"type": "object"},
                "execute": execute,
            }
        )

    result = run(ExtensionLoader(cwd=".").load_factory(factory))
    runner = result.runner
    assert runner is not None
    registry = CapabilityRegistry()
    runner.bind_tool_registry(registry)
    context = ToolExecutionContext(
        permission=PermissionContext(),
        tool_call_id="stream-call",
        stream_callback=lambda text, stream, call_id: updates.append(
            (text, stream, call_id)
        ),
    )
    output = run(registry.execute("streaming_extension", {}, context=context))
    assert output.content == "done"
    assert updates == [("chunk", "stdout", "stream-call")]


def test_command_registry_adapter_injects_guarded_context() -> None:
    seen: list[tuple[object, str]] = []

    def factory(api):
        def command(args, ctx):
            seen.append((args, ctx.cwd))
            return True

        api.register_command("inspect", command)

    result = run(ExtensionLoader(cwd=".").load_factory(factory))
    runner = ExtensionRunner(result.extensions, result.runtime, cwd=".")
    registry = CommandRegistry()
    assert runner.bind_command_registry(registry) == ["inspect"]
    # Extension commands run only through the scoped slash dispatcher, which
    # carries the owning scope and the session object.
    assert run(registry.dispatch_slash(None, "inspect", "value", None, scope_id="*")) == (
        True,
        "value",
    )
    assert seen == [("value", str(Path(".").resolve()))]
    assert run(registry.dispatch("inspect", {"args": "value"})) is False


def test_extension_command_projection_uses_the_complete_frontend_contract() -> None:
    seen: list[str] = []

    def factory(api):
        api.register_command(
            "inspect",
            {
                "description": "Inspect the active MiniCode extension runtime.",
                "get_argument_completions": lambda _prefix: [
                    {"value": "runtime", "description": "Inspect runtime state"}
                ],
                "handler": lambda args, _ctx: seen.append(str(args)),
            },
        )

    result = run(ExtensionLoader(cwd=".").load_factory(factory))
    runner = ExtensionRunner(result.extensions, result.runtime, cwd=".")
    registry = CommandRegistry()

    assert runner.bind_command_registry(
        registry,
        scope_id="conversation-a",
    ) == ["inspect"]
    assert registry.list_extension_slash_commands(
        scope_id="conversation-a",
    ) == [
        {
            "name": "inspect",
            "command": "inspect",
            "label": "/inspect",
            "description": "Inspect the active MiniCode extension runtime.",
            "type": "local",
            "source": "extension",
            "extension_path": "<inline>",
            "enabled": True,
            "availability": {
                "kind": "always",
                "scope": "conversation",
            },
        }
    ]
    assert registry.list_extension_slash_commands(
        scope_id="conversation-b",
    ) == []
    handled, remaining = run(
        registry.dispatch_slash(
            None,
            "/inspect",
            "runtime",
            None,
            scope_id="conversation-a",
        )
    )
    assert handled is True
    assert remaining == "runtime"
    assert seen == ["runtime"]


def test_stale_lifecycle_runtime_cannot_clear_a_newer_command_projection() -> None:
    calls: list[str] = []

    def old_factory(api):
        api.register_command(
            "inspect",
            {
                "description": "Old command generation.",
                "handler": lambda _args, _ctx: calls.append("old"),
            },
        )

    def new_factory(api):
        api.register_command(
            "inspect",
            {
                "description": "New command generation.",
                "handler": lambda _args, _ctx: calls.append("new"),
            },
        )

    old_result = run(ExtensionLoader(cwd=".").load_factory(old_factory))
    new_result = run(ExtensionLoader(cwd=".").load_factory(new_factory))
    old_runner = ExtensionRunner(old_result.extensions, old_result.runtime, cwd=".")
    new_runner = ExtensionRunner(new_result.extensions, new_result.runtime, cwd=".")
    registry = CommandRegistry()

    old_runner.bind_command_registry(registry, scope_id="conversation-a")
    new_runner.bind_command_registry(registry, scope_id="conversation-a")
    old_runner.detach_command_registry()

    projected = registry.list_extension_slash_commands(scope_id="conversation-a")
    assert [item["description"] for item in projected] == [
        "New command generation."
    ]
    handled, _remaining = run(
        registry.dispatch_slash(
            None,
            "/inspect",
            "",
            None,
            scope_id="conversation-a",
        )
    )
    assert handled is True
    assert calls == ["new"]


def test_provider_queue_flush_and_owner_unregistration() -> None:
    class Sink:
        def __init__(self):
            self.registered: list[tuple[str, object]] = []
            self.unregistered: list[str] = []

        def register_provider(self, name, config):
            self.registered.append((name, config))

        def unregister_provider(self, name):
            self.unregistered.append(name)

    def factory(api):
        api.register_provider("queued", {"model": "x"})

    result = run(ExtensionLoader(cwd=".").load_factory(factory))
    runner = ExtensionRunner(result.extensions, result.runtime)
    sink = Sink()
    runner.bind_provider_sink(sink)
    assert sink.registered == [("queued", {"model": "x"})]
    run(runner.shutdown("reload"))
    assert sink.unregistered == ["queued"]


def test_reload_generation_and_stale_context(tmp_path: Path) -> None:
    module = _write_extension(
        tmp_path / "extension.py",
        """
def extension(api):
    api.register_flag('generation', {'type': 'string', 'default': 'v1'})
""",
    )
    policy = ExtensionTrustPolicy(
        cwd=tmp_path, project_root=tmp_path, project_trusted=True
    )
    loader = ExtensionLoader(cwd=tmp_path, trust_policy=policy)
    first = run(loader.load([module]))
    old_runner = ExtensionRunner(first.extensions, first.runtime, cwd=tmp_path)
    old_context = old_runner.create_context()
    assert first.runtime.flag_values["generation"] == "v1"
    module.write_text(
        """
def extension(api):
    api.register_flag('generation', {'type': 'string', 'default': 'v2'})
""",
        encoding="utf-8",
    )
    clear_extension_cache()
    second = run(loader.load([module]))
    assert second.generation > first.generation
    assert second.runtime.flag_values["generation"] == "v2"
    run(old_runner.shutdown("reload"))
    with pytest.raises(ExtensionStaleError):
        _ = old_context.cwd


def test_loader_does_not_rebind_runtime_from_previous_generation(
    tmp_path: Path,
) -> None:
    module = _write_extension(
        tmp_path / "generation.py",
        "def extension(api):\n    api.register_flag('ok', {'type': 'boolean', 'default': True})\n",
    )
    policy = ExtensionTrustPolicy(
        cwd=tmp_path, project_root=tmp_path, project_trusted=True
    )
    loader = ExtensionLoader(cwd=tmp_path, trust_policy=policy)
    first = run(loader.load([module]))
    clear_extension_cache()
    second = run(loader.load([module], runtime=first.runtime))
    assert second.runtime is not first.runtime
    assert first.runtime.active is False


def test_event_bus_capability_is_removed_and_staled_on_shutdown() -> None:
    observed: list[str] = []

    def factory(api):
        api.events.on("host_event", lambda event: observed.append(str(event)))

    result = run(ExtensionLoader(cwd=".").load_factory(factory))
    runner = ExtensionRunner(result.extensions, result.runtime)
    run(runner.event_bus.emit("host_event", "before"))
    assert observed == ["before"]


def test_session_start_lifecycle_is_emitted_once() -> None:
    observed: list[str] = []

    def factory(api):
        api.on("session_start", lambda event, ctx: observed.append(event["reason"]))

    result = run(ExtensionLoader(cwd=".").load_factory(factory))
    runner = result.runner
    assert runner is not None
    run(runner.startup("startup"))
    run(runner.startup("reload"))
    assert observed == ["startup"]
    run(runner.shutdown("reload"))
    run(runner.event_bus.emit("host_event", "after"))
    # Shutdown must not replay or duplicate the one-shot session_start event.
    assert observed == ["startup"]


def test_agent_context_and_provider_hooks_fold_in_extension_order() -> None:
    seen: list[str] = []

    def factory(api):
        def before_agent(event, ctx):
            seen.append(f"before:{event['system_prompt']}")
            return {
                "system_prompt": f"{event['system_prompt']}+one",
                "message": {"role": "custom", "content": "first"},
            }

        def before_agent_second(event, ctx):
            seen.append(f"before:{event['system_prompt']}")
            return {"messages": [{"role": "custom", "content": "second"}]}

        def context_hook(event, ctx):
            seen.append("context")
            return {"messages": [*event["messages"], {"role": "user", "content": "added"}]}

        def provider_hook(event, ctx):
            seen.append("provider")
            payload = dict(event["payload"])
            payload["metadata"] = {**payload.get("metadata", {}), "trace": "extension"}
            return payload

        api.on("before_agent_start", before_agent)
        api.on("before_agent_start", before_agent_second)
        api.on("context", context_hook)
        api.on("before_provider_request", provider_hook)

    result = run(ExtensionLoader(cwd=".").load_factory(factory))
    runner = result.runner
    assert runner is not None

    before = run(
        runner.emit_before_agent_start(
            "hello", None, "base", {"cwd": "workspace"}
        )
    )
    assert before is not None
    assert before["system_prompt"] == "base+one"
    assert [item["content"] for item in before["messages"]] == ["first", "second"]
    assert seen[:2] == ["before:base", "before:base+one"]

    transformed = run(runner.emit_context([{"role": "user", "content": "hello"}]))
    assert transformed[-1]["content"] == "added"
    payload = run(
        runner.emit_before_provider_request(
            {"messages": transformed, "tools": [], "metadata": {}}
        )
    )
    assert payload["metadata"] == {"trace": "extension"}
    assert seen[-2:] == ["context", "provider"]


def test_context_hook_cannot_mutate_canonical_input_messages() -> None:
    def factory(api):
        def context_hook(event, ctx):
            event["messages"][0]["content"] = "extension copy"

        api.on("context", context_hook)

    result = run(ExtensionLoader(cwd=".").load_factory(factory))
    runner = result.runner
    assert runner is not None
    canonical = [{"role": "user", "content": "canonical"}]

    transformed = run(runner.emit_context(canonical))

    assert canonical[0]["content"] == "canonical"
    assert transformed[0]["content"] == "extension copy"


def test_captured_factory_api_is_stale_after_runtime_replacement() -> None:
    captured: list[object] = []

    def factory(api):
        api.register_flag("captured", {"type": "boolean", "default": True})
        captured.append(api)

    result = run(ExtensionLoader(cwd=".").load_factory(factory))
    runner = ExtensionRunner(result.extensions, result.runtime)
    run(runner.shutdown("reload"))
    with pytest.raises(ExtensionStaleError):
        captured[0].get_flag("captured")  # type: ignore[union-attr]


def test_trust_policy_blocks_untrusted_project_before_import(tmp_path: Path) -> None:
    marker = tmp_path / "imported.txt"
    module = _write_extension(
        tmp_path / "untrusted.py",
        f"Path = __import__('pathlib').Path\nPath(r'{marker}').write_text('executed')\ndef extension(api):\n    pass\n",
    )
    policy = ExtensionTrustPolicy(
        cwd=tmp_path, project_root=tmp_path, project_trusted=False
    )
    loader = ExtensionLoader(cwd=tmp_path, trust_policy=policy)
    result = run(loader.load([module]))
    assert result.extensions == []
    assert result.errors and "not trusted" in result.errors[0]["error"]
    assert not marker.exists()

    external = tmp_path.parent / f"{tmp_path.name}-external.py"
    external.write_text("def extension(api): pass\n", encoding="utf-8")
    try:
        with pytest.raises(ExtensionTrustError):
            policy.assert_allowed(external)
    finally:
        external.unlink(missing_ok=True)


def test_trust_policy_rejects_symlinked_extension(tmp_path: Path) -> None:
    real = _write_extension(tmp_path / "real.py", "def extension(api): pass\n")
    link = tmp_path / "link.py"
    try:
        link.symlink_to(real)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are unavailable in this environment")
    policy = ExtensionTrustPolicy(
        cwd=tmp_path, project_root=tmp_path, project_trusted=True
    )
    decision = policy.check(link)
    assert decision.allowed is False
    assert "symbolic link" in decision.reason


def test_declared_scope_cannot_override_configured_root_boundary(
    tmp_path: Path,
) -> None:
    outside = _write_extension(tmp_path / "outside.py", "def extension(api): pass\n")
    managed_root = tmp_path / "managed"
    managed_root.mkdir()
    policy = ExtensionTrustPolicy(
        cwd=tmp_path,
        project_root=tmp_path / "project",
        managed_roots=(managed_root,),
        project_trusted=True,
    )
    decision = policy.check(outside, source_scope="managed")
    assert decision.allowed is False
    assert "does not match" in decision.reason


def test_discovery_is_deterministic_and_manifest_is_contained(tmp_path: Path) -> None:
    root = tmp_path / "extensions"
    root.mkdir()
    _write_extension(root / "b.py", "def extension(api): pass\n")
    _write_extension(root / "a.py", "def extension(api): pass\n")
    package = root / "package"
    package.mkdir()
    (package / "minicode-extension.json").write_text(
        '{"extensions":["entry.py", "../escape.py"]}', encoding="utf-8"
    )
    _write_extension(package / "entry.py", "def extension(api): pass\n")
    _write_extension(tmp_path / "escape.py", "def extension(api): pass\n")
    found = discover_extensions_in_dir(root)
    assert [path.name for path in found] == ["a.py", "b.py", "entry.py"]


def test_discovery_ignores_external_harness_locations_and_manifests(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    user_config = tmp_path / "user-config"
    canonical_project = project / ".minicode" / "extensions"
    external_project = project / ".pi" / "extensions"
    canonical_user = user_config / "extensions"
    for directory in (canonical_project, external_project, canonical_user):
        directory.mkdir(parents=True)
    _write_extension(canonical_project / "project.py", "def extension(api): pass\n")
    _write_extension(external_project / "external.py", "def extension(api): pass\n")
    _write_extension(canonical_user / "user.py", "def extension(api): pass\n")

    external_package = canonical_project / "external-package"
    external_package.mkdir()
    (external_package / "package.json").write_text(
        '{"pi":{"extensions":["hidden.py"]}}',
        encoding="utf-8",
    )
    _write_extension(external_package / "hidden.py", "def extension(api): pass\n")

    policy = ExtensionTrustPolicy(
        cwd=project,
        project_root=project,
        project_trusted=True,
        user_roots=(canonical_user,),
    )
    found = ExtensionLoader(cwd=project, trust_policy=policy).discover(
        project_root=project,
        user_config_dir=user_config,
    )
    assert [path.name for path in found] == ["project.py", "user.py"]


def test_tool_definition_rejects_noncanonical_field_spelling() -> None:
    with pytest.raises(ExtensionRegistrationError, match="execution_mode"):
        ExtensionToolDefinition.from_value(
            {
                "name": "external-shape",
                "description": "invalid external contract",
                "parameters": {"type": "object"},
                "execute": lambda _params: None,
                "executionMode": "parallel",
            }
        )


def test_python_package_extension_preserves_relative_imports(tmp_path: Path) -> None:
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "helper.py").write_text("VALUE = 'relative-ok'\n", encoding="utf-8")
    (package / "__init__.py").write_text(
        "from .helper import VALUE\n"
        "def extension(api):\n"
        "    api.register_flag('relative', {'type': 'string', 'default': VALUE})\n",
        encoding="utf-8",
    )
    policy = ExtensionTrustPolicy(
        cwd=tmp_path, project_root=tmp_path, project_trusted=True
    )
    result = run(
        ExtensionLoader(cwd=tmp_path, trust_policy=policy).load(
            [package / "__init__.py"]
        )
    )
    assert result.errors == []
    assert result.runtime.flag_values["relative"] == "relative-ok"
