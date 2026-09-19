from __future__ import annotations

import asyncio
import json

import pytest

from backend.agent.state import AgentState
from backend.llm.base import ToolCallEvent
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.config import PermissionSettings
from backend.permissions.checker import PermissionChecker
from backend.extensions.lifecycle_observer import lifecycle_observer_factory
from backend.tests.test_extension_model_ownership import bind
from backend.tests.test_model_execution_ownership import execute, setup
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema
from backend.tools.registry import ToolRegistry
from backend.tools.tool_search import ToolSearchTool


class _SnapshotTool(BaseTool):
    name = "snapshot_tool"
    description = "Return the implementation label used by this request."
    permission = PermissionLevel.AUTO

    def __init__(self, label: str) -> None:
        self.label = label
        self.calls = 0

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={"type": "object", "properties": {}},
        )

    async def execute(self, args, context=None) -> ToolResult:
        self.calls += 1
        return ToolResult(self.label)


class _DeferredTool(BaseTool):
    name = "optional_snapshot_tool"
    description = "Optional snapshot tool."
    permission = PermissionLevel.AUTO
    should_defer = True
    search_hint = "optional snapshot"

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={"type": "object", "properties": {}},
        )

    async def execute(self, args, context=None) -> ToolResult:
        return ToolResult("optional")


@pytest.mark.asyncio
async def test_provider_step_executes_the_advertised_tool_after_live_replacement(
    tmp_path, monkeypatch
):
    old = _SnapshotTool("old-step-implementation")
    replacement = _SnapshotTool("replacement-implementation")
    fixture = None
    requests = 0
    observed_tool_messages = []

    async def behavior(model, messages):
        nonlocal requests
        requests += 1
        if requests == 1:
            # This is a host update while the provider response is being
            # admitted. The current step must retain the tool it advertised;
            # the replacement belongs to the next step.
            fixture.session.tool_registry.register(
                replacement,
                replace=True,
                owner="replacement",
            )
            return ToolCallEvent(
                id="snapshot-call",
                name="snapshot_tool",
                arguments={},
            )
        tool_messages = [message.content for message in messages if message.role == "tool"]
        assert tool_messages, messages
        observed_tool_messages.append(tool_messages[-1])
        if requests == 2:
            assert "old-step-implementation" in tool_messages[-1], tool_messages
            return ToolCallEvent(
                id="snapshot-call-next-step",
                name="snapshot_tool",
                arguments={},
            )
        assert "replacement-implementation" in tool_messages[-1], tool_messages
        return None

    fixture = setup(tmp_path, monkeypatch, behavior, extra_tools=[old])
    await execute(fixture, tmp_path)

    assert requests == 3
    assert old.calls == 1
    assert replacement.calls == 1
    assert len(observed_tool_messages) == 2
    assert "old-step-implementation" in observed_tool_messages[0]
    assert "replacement-implementation" in observed_tool_messages[1]
    assert fixture.session.tool_registry.get_tool("snapshot_tool") is replacement


def test_deferred_discovery_uses_the_admitted_registry_snapshot():
    base = ToolRegistry()
    base.register(ToolSearchTool(base))
    optional = _DeferredTool()
    base.register(optional)
    admitted = base.fork()
    admitted.unregister(optional.name)
    state = AgentState(user_message="find an optional tool")
    context = ToolExecutionContext(
        permission=PermissionContext(mode="bypass"),
        metadata={"_agent_state": state},
        tool_registry=admitted,
    )

    result = asyncio.run(
        ToolSearchTool(base).execute(
            {"query": f"select:{optional.name}"},
            context=context,
        )
    )

    payload = json.loads(result.content)
    assert payload["matches"] == []
    assert payload["activated"] == []
    assert optional.name in base.list_tools()
    assert optional.name not in admitted.list_tools()


def _cell_report(messages):
    content = next(message.content for message in reversed(messages) if message.role == "tool")
    return json.JSONDecoder().raw_decode(content[content.index("{"):])[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("revoke_permission", [False, True])
async def test_yielded_cell_keeps_its_registry_and_selection_but_honors_live_permissions(tmp_path, monkeypatch, revoke_permission):
    ready = asyncio.Event()
    class Gate(_SnapshotTool):
        name = "gate"
        read_only = True
        async def execute(self, args, context=None):
            await ready.wait()
            return ToolResult("ready")

    old, new = _SnapshotTool("old"), _SnapshotTool("new")
    optional = _DeferredTool()
    fixture = None
    reports = []
    async def behavior(model, messages):
        if len(model.inputs) == 1:
            return ToolCallEvent(id="old-cell", name="tool_exec", arguments={"yield_time_ms": 0, "code":
                'await tools.gate({}); text(JSON.parse((await tools.tool_search({query:"select:optional_snapshot_tool"})).content).matches); '
                'const r=await tools.snapshot_tool({}); text({content:r.content,error:r.is_error});'})
        report = _cell_report(messages)
        reports.append(report)
        if len(model.inputs) == 2:
            assert report["status"] == "running"
            fixture.session.tool_registry.register(new, replace=True)
            fixture.session.tool_registry.unregister(optional.name)
            fixture.session.active_tool_names = ("tool_wait",)
            if revoke_permission:
                fixture.owner.permission_context_provider = lambda: PermissionContext(mode="confirm", tool_deny_rules=["snapshot_tool"])
            ready.set()
            return ToolCallEvent(id="join-old-cell", name="tool_wait", arguments={"cell_id": report["cell_id"], "yield_time_ms": 10000})
        return None

    fixture = setup(tmp_path, monkeypatch, behavior, [old, Gate("gate"), optional])
    fixture.session.tool_registry.register(ToolSearchTool(fixture.session.tool_registry))
    await execute(fixture, tmp_path)
    final = reports[-1]
    assert final["status"] == "completed", reports
    assert json.loads(final["output"][0]) == [optional.name]
    result = json.loads(final["output"][1])
    assert result["error"] is revoke_permission
    assert old.calls == (0 if revoke_permission else 1)
    assert new.calls == 0
    if not revoke_permission:
        assert result["content"] == "old"


@pytest.mark.asyncio
async def test_extension_exec_from_an_old_cell_uses_its_calling_tool_plan(tmp_path, monkeypatch):
    ready = asyncio.Event()
    class Gate(_SnapshotTool):
        name = "gate"
        read_only = True
        async def execute(self, args, context=None):
            await ready.wait()
            return ToolResult("ready")
    class Command(_SnapshotTool):
        name = "run_command"
        def __init__(self, label):
            super().__init__(label)
            self.description = label

    old, new = Command("old command"), Command("new command")
    fixture = None
    async def behavior(model, messages):
        if len(model.inputs) == 1:
            return ToolCallEvent(id="cell", name="tool_exec", arguments={"yield_time_ms": 0,
                "code": 'await tools.gate({}); text((await tools.wrapper({})).content);'})
        report = _cell_report(messages)
        if len(model.inputs) == 2:
            fixture.session.tool_registry.register(new, replace=True)
            fixture.session.active_tool_names = ("tool_wait",)
            ready.set()
            return ToolCallEvent(id="join", name="tool_wait", arguments={"cell_id": report["cell_id"], "yield_time_ms": 10000})
        assert report["status"] == "completed", report
        assert report["output"] == ["old command"], report
        return None

    def factory(api):
        async def wrapper(args, ctx):
            inventory = {item["name"]: item for item in api.get_all_tools()}
            assert inventory["run_command"]["description"] == "old command"
            assert "run_command" in api.get_active_tools()
            return await api.exec("fixture-only")
        api.register_tool({"name": "wrapper", "description": "Nested extension command", "read_only": True,
                           "parameters": {"type": "object"}, "execute": wrapper})

    fixture = setup(tmp_path, monkeypatch, behavior, [old, Gate("gate")])
    fixture.session.permission_checker = PermissionChecker(PermissionSettings(auto_allow=["*"], require_confirm=[]), tmp_path)
    runner = await bind(fixture, tmp_path, factory)
    fixture.owner.lifecycle_runtime = runner
    fixture.session.lifecycle_observer_factory = lifecycle_observer_factory
    try:
        await execute(fixture, tmp_path)
        assert old.calls == 1
        assert new.calls == 0
    finally:
        runner.invalidate()


@pytest.mark.asyncio
async def test_late_registration_can_be_selected_for_the_next_request(tmp_path, monkeypatch):
    fixture = None
    added = _SnapshotTool("added")
    async def behavior(model, messages):
        if len(model.inputs) == 1:
            return ToolCallEvent(id="register", name="register_next", arguments={})
        if len(model.inputs) == 2:
            return ToolCallEvent(id="new-tool", name=added.name, arguments={})
        return None
    def factory(api):
        async def register_next(args, ctx):
            fixture.session.tool_registry.register(added)
            api.set_active_tools([added.name])
            return "registered"
        api.register_tool({"name": "register_next", "description": "Register next step tool", "read_only": True,
                           "parameters": {"type": "object"}, "execute": register_next})
    fixture = setup(tmp_path, monkeypatch, behavior)
    runner = await bind(fixture, tmp_path, factory)
    fixture.owner.lifecycle_runtime = runner
    fixture.session.lifecycle_observer_factory = lifecycle_observer_factory
    try:
        await execute(fixture, tmp_path)
        assert added.calls == 1
        assert fixture.session.active_tool_names == (added.name,)
    finally:
        runner.invalidate()
