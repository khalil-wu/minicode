from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from backend.agent.message import AgentEvent
from backend.agent.runtime import default_runtime
from backend.config import AppConfig, LLMSettings
from backend.extensions.loader import ExtensionLoader, clear_extension_cache
from backend.tests.test_agent_runner_done_fallback import _NoopLLM, _Session
from backend.tools.registry import ToolRegistry
from backend.ws.agent_runner import SessionAgentRunnerMixin


class _ExtensionHost(SessionAgentRunnerMixin):
    def __init__(self) -> None:
        self.session_id = "session-extension-composition"
        self.command_registry = None
        self.run_manager = SimpleNamespace(run_tasks={})


def _write_project_extension(
    workspace: Path,
    *,
    lifecycle_marker: Path,
    import_marker: Path | None = None,
) -> Path:
    extension_dir = workspace / ".minicode" / "extensions"
    extension_dir.mkdir(parents=True)
    extension_path = extension_dir / "project_extension.py"
    import_probe = (
        f"Path({str(import_marker)!r}).write_text('imported', encoding='utf-8')\n"
        if import_marker is not None
        else ""
    )
    extension_path.write_text(
        "from pathlib import Path\n"
        f"{import_probe}"
        f"LIFECYCLE = Path({str(lifecycle_marker)!r})\n"
        "\n"
        "def _record(value):\n"
        "    previous = LIFECYCLE.read_text(encoding='utf-8') if LIFECYCLE.exists() else ''\n"
        "    LIFECYCLE.write_text(previous + value + '\\n', encoding='utf-8')\n"
        "\n"
        "def extension(api):\n"
        "    api.register_tool({\n"
        "        'name': 'project_echo',\n"
        "        'description': 'Echo a value from the project extension.',\n"
        "        'parameters': {\n"
        "            'type': 'object',\n"
        "            'properties': {'value': {'type': 'string'}},\n"
        "        },\n"
        "        'execute': lambda params, ctx=None: {'content': params.get('value', '')},\n"
        "    })\n"
        "    api.on('session_start', lambda event, ctx: _record('start:' + event['reason']))\n"
        "    api.on('session_shutdown', lambda event, ctx: _record('shutdown:' + event['reason']))\n",
        encoding="utf-8",
    )
    return extension_path


def _isolate_user_extension_roots(monkeypatch: Any, tmp_path: Path) -> None:
    monkeypatch.setenv("MINICODE_CONFIG_DIR", str(tmp_path / "isolated-minicode-home"))


def _patch_agent_run_config(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        "backend.ws.agent_runner.load_config",
        lambda cwd=None: AppConfig(llm=LLMSettings(api_key="test-key")),
    )
    monkeypatch.setattr(
        "backend.ws.agent_runner.get_llm_provider",
        lambda: "openai",
    )
    monkeypatch.setattr(
        "backend.ws.agent_runner.get_available_models",
        lambda provider="openai": ["gpt-test"],
    )
    monkeypatch.setattr(
        "backend.llm.model_registry.create_session_llm",
        lambda config, model_override=None, **_kwargs: _NoopLLM(),
    )


def test_ws_composition_discovers_trusted_project_extension_and_shuts_it_down(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    clear_extension_cache()
    _isolate_user_extension_roots(monkeypatch, tmp_path)
    lifecycle_marker = tmp_path / "lifecycle.txt"
    _write_project_extension(
        tmp_path,
        lifecycle_marker=lifecycle_marker,
    )
    trust_checks: list[Path] = []

    def trusted(root: Path) -> bool:
        trust_checks.append(Path(root))
        return True

    monkeypatch.setattr("backend.ws.agent_runner.is_workspace_trusted", trusted)
    host = _Session(tmp_path, [])
    registry = ToolRegistry()

    async def scenario() -> None:
        runtime = await host._ensure_lifecycle_runtime(
            conversation_id="conv_runnerdone",
            workspace_root=tmp_path,
            tool_registry=registry,
        )

        assert runtime is not None
        state = host._extension_runtime_state("conv_runnerdone")
        result = state["result"]
        assert runtime is result.runner
        assert runtime.mode == "rpc"
        assert runtime._tool_registry is registry
        assert registry.get_tool("project_echo") is not None
        assert len(result.extensions) == 1
        source = result.extensions[0].source
        assert source.scope == "project"
        assert source.trusted is True
        assert lifecycle_marker.read_text(encoding="utf-8").splitlines() == [
            "start:startup"
        ]

        await host._shutdown_lifecycle_runtimes("test_shutdown")

        assert runtime.active is False
        assert registry.get_tool("project_echo") is None
        assert host._extension_runtime_states == {}
        assert lifecycle_marker.read_text(encoding="utf-8").splitlines() == [
            "start:startup",
            "shutdown:test_shutdown",
        ]

    asyncio.run(scenario())
    assert trust_checks == [tmp_path]
    clear_extension_cache()


def test_ws_composition_denies_untrusted_project_before_source_execution(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    clear_extension_cache()
    _isolate_user_extension_roots(monkeypatch, tmp_path)
    lifecycle_marker = tmp_path / "lifecycle.txt"
    import_marker = tmp_path / "imported.txt"
    _write_project_extension(
        tmp_path,
        lifecycle_marker=lifecycle_marker,
        import_marker=import_marker,
    )
    monkeypatch.setattr(
        "backend.ws.agent_runner.is_workspace_trusted",
        lambda root: False,
    )
    host = _Session(tmp_path, [])
    registry = ToolRegistry()

    async def scenario() -> None:
        runtime = await host._ensure_lifecycle_runtime(
            conversation_id="conv_runnerdone",
            workspace_root=tmp_path,
            tool_registry=registry,
        )

        assert runtime is not None
        result = host._extension_runtime_state("conv_runnerdone")["result"]
        assert result.extensions == []
        assert result.errors
        assert "project is not trusted" in result.errors[0]["error"]
        assert registry.get_tool("project_echo") is None
        assert not import_marker.exists()
        assert not lifecycle_marker.exists()

        await host._shutdown_lifecycle_runtimes("test_cleanup")

    asyncio.run(scenario())
    clear_extension_cache()


def test_ws_run_injects_the_conversation_owned_lifecycle_runtime(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    events: list[dict[str, Any]] = []
    session = _Session(tmp_path, events)
    # The injection contract is independent of workspace memory startup. Keep
    # this focused test at the WS composition seam.
    session.session_lifecycle.workspace_root_for_conversation = (
        lambda conversation=None: None
    )
    runner = SimpleNamespace(active=True)
    captured: dict[str, Any] = {}

    async def ensure_lifecycle_runtime(
        *,
        conversation_id: str,
        workspace_root: Path | None,
        tool_registry: Any,
    ):
        captured["conversation_id"] = conversation_id
        captured["workspace_root"] = workspace_root
        captured["tool_registry"] = tool_registry
        return runner

    class CaptureQueryEngine:
        async def submit(self, submission):
            captured["runtime"] = submission.runtime
            captured["agent_session"] = submission.session
            captured["run_context"] = submission.runtime.run_context
            yield AgentEvent.agent_message_completed(
                "Extension runner reached the turn.",
                source="model_final",
            )
            yield AgentEvent.done()

    session._ensure_lifecycle_runtime = ensure_lifecycle_runtime
    session.query_engine = CaptureQueryEngine()
    _patch_agent_run_config(monkeypatch)

    asyncio.run(
        session._run_agent_locked(
            "Use the extension runtime",
            conversation_id="conv_runnerdone",
            metadata={
                "agent_runtime": default_runtime(),
                "assistant_message_id": "assistant-extension-runtime",
                "user_message_id": "user-extension-runtime",
            },
        )
    )

    runtime = captured["runtime"]
    assert captured["conversation_id"] == "conv_runnerdone"
    assert captured["workspace_root"] is None
    assert captured["tool_registry"] is captured["agent_session"].tool_registry
    assert runtime.lifecycle_runtime is runner
    assert captured["run_context"].lifecycle_runtime is runner
    assert "_extension_runner" not in runtime.metadata
    assert "_extensions_result" not in runtime.metadata


def test_inactive_conversation_provider_registration_still_runs_offline_model_refresh() -> None:
    calls: list[dict[str, Any]] = []

    class Runtime:
        active = True

        async def refresh_dynamic_models(self, **kwargs: Any) -> None:
            calls.append(dict(kwargs))

        def refresh(self) -> None:  # pragma: no cover - compatibility fallback
            raise AssertionError("dynamic provider refresh should be used")

    runtime = Runtime()
    host = _ExtensionHost()
    host.active_conversation_id = "visible-conversation"
    state = host._extension_runtime_state("background-conversation")
    state["model_runtime"] = runtime

    asyncio.run(
        host._refresh_model_runtime_projection(
            "background-conversation",
            runtime,
            provider_id="extension-provider",
            action="register",
        )
    )

    assert calls == [{"allow_network": False, "force": True}]


def test_extension_candidate_cannot_publish_after_session_shutdown(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    clear_extension_cache()
    _isolate_user_extension_roots(monkeypatch, tmp_path)
    lifecycle_marker = tmp_path / "lifecycle.txt"
    _write_project_extension(tmp_path, lifecycle_marker=lifecycle_marker)
    monkeypatch.setattr(
        "backend.ws.agent_runner.is_workspace_trusted",
        lambda root: True,
    )
    host = _Session(tmp_path, [])
    registry = ToolRegistry()
    candidate_loaded = asyncio.Event()
    release_candidate = asyncio.Event()
    original_load = ExtensionLoader.load

    async def delayed_publish(self, *args: Any, **kwargs: Any):
        result = await original_load(self, *args, **kwargs)
        candidate_loaded.set()
        await release_candidate.wait()
        return result

    monkeypatch.setattr(ExtensionLoader, "load", delayed_publish)

    async def scenario() -> None:
        ensure_task = asyncio.create_task(
            host._ensure_lifecycle_runtime(
                conversation_id="conv_runnerdone",
                workspace_root=tmp_path,
                tool_registry=registry,
            )
        )
        await candidate_loaded.wait()
        await host._shutdown_lifecycle_runtimes("disconnect")
        release_candidate.set()

        assert await ensure_task is None
        assert registry.get_tool("project_echo") is None
        assert getattr(host, "_extension_runtime_states", {}) == {}

    asyncio.run(scenario())
    clear_extension_cache()
