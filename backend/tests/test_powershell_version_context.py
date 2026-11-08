from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import psutil
import pytest

from backend.agent.context import ContextBuilder
from backend.agent.environment_selection import TurnEnvironment
from backend.agent.model_execution import ModelExecutionSnapshot
from backend.agent.run_context import RunContext
from backend.agent.runtime import AgentRuntime
from backend.agent.shell_version import parse_powershell_version, powershell_version, shell_version_for_environment
from backend.agent.state import AgentState
from backend.artifact.store import ArtifactStore
from backend.config import AppConfig, LLMSettings
from backend.feature_flags import FeatureFlags
from backend.permissions.context import PermissionContext
from backend.sdk import create_session
from backend.tests.test_thread_sample import Model, done
from backend.llm.base import StreamEvent, StreamEventType
from backend.tools.registry import ToolRegistry


@pytest.mark.parametrize("raw,expected", [(b"7.5.2\r\n", "7.5"), (b"5.1.26100.7462", "5.1"),
    (b"+7.0005", "7.5"), (b"7", None), (b"65536.1", None), (b"-1.0", None),
    (b"7 .5", None), (b"\xff.1", None), (b"7.5" + b" " * 62, None)])
def test_source_version_parser_requires_bounded_utf8_and_u16_components(raw, expected):
    assert parse_powershell_version(raw) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled,kind,count", [(False, "powershell", 1), (True, "bash", 1),
    (True, "powershell", 0), (True, "powershell", 2)])
async def test_disabled_or_non_single_powershell_does_not_launch_a_probe(monkeypatch, enabled, kind, count):
    probe = AsyncMock(side_effect=AssertionError("Ineligible environment must not probe a host executable"))
    monkeypatch.setattr("backend.agent.shell_version.powershell_version", probe)
    environment = TurnEnvironment(None, "Thread", SimpleNamespace(environment=object()), {"shell": {"name": kind, "path": "owned-shell"}})
    assert await shell_version_for_environment(SimpleNamespace(environments=(environment,) * count), enabled=enabled) is None
    probe.assert_not_awaited()


@pytest.mark.asyncio
async def test_remote_environment_never_probes_its_shell_path_on_controller(monkeypatch):
    from backend.environments.manager import Environment
    environment = TurnEnvironment(None, "Thread", SimpleNamespace(environment=object.__new__(Environment)),
        {"shell": {"name": "powershell", "path": "C:/remote/pwsh.exe"}})
    probe = AsyncMock(side_effect=AssertionError("Remote path cannot launch locally"))
    monkeypatch.setattr("backend.agent.shell_version.powershell_version", probe)
    assert await shell_version_for_environment(SimpleNamespace(environments=(environment,)), enabled=True) is None
    probe.assert_not_awaited()


@pytest.mark.asyncio
async def test_probe_caches_success_and_failure_by_actual_executable_path(monkeypatch):
    from backend.agent import shell_version
    from backend.subprocesses import spawn_exec
    monkeypatch.setattr(shell_version, "_POWERSHELL_VERSIONS", {})
    calls = []
    async def spawn(path, *args, **kwargs):
        calls.append((path, args))
        return await spawn_exec(sys.executable, "-c", "print('7.5.2')" if path == "good" else "print('invalid')", **kwargs)
    monkeypatch.setattr(shell_version, "spawn_exec", spawn)
    for _ in range(2):
        assert await powershell_version("good") == "7.5"
        assert await powershell_version("bad") is None
    assert [path for path, _ in calls] == ["good", "bad"]
    assert all(args == ("-NoProfile", "-NonInteractive", "-Command", "$PSVersionTable.PSVersion.ToString()") for _, args in calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_probe_timeout_and_cancellation_reap_owned_child(monkeypatch, cancel):
    from backend.agent import shell_version
    from backend.subprocesses import spawn_exec
    monkeypatch.setattr(shell_version, "_POWERSHELL_VERSIONS", {})
    monkeypatch.setattr(shell_version, "_VERSION_TIMEOUT", .3)
    spawned = asyncio.Event()
    processes = []
    async def spawn(*args, **kwargs):
        process = await spawn_exec(sys.executable, "-c", "import time;time.sleep(60)", **kwargs)
        processes.append(process)
        spawned.set()
        return process
    monkeypatch.setattr(shell_version, "spawn_exec", spawn)
    task = asyncio.create_task(powershell_version("slow-shell"))
    await asyncio.wait_for(spawned.wait(), 5)
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert "slow-shell" not in shell_version._POWERSHELL_VERSIONS
    else:
        assert await task is None
        assert shell_version._POWERSHELL_VERSIONS == {"slow-shell": None}
    assert processes[0].returncode is not None and not psutil.pid_exists(processes[0].pid)


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "nt", reason="Actual local PowerShell version and prompt on Windows")
async def test_real_query_projects_actual_powershell_version_only_when_feature_enabled(tmp_path, record_testsuite_property):
    from backend.runtime_env import default_user_shell
    selected = default_user_shell(native_process=True)
    assert selected["name"] == "powershell"
    version = await powershell_version(selected["path"])
    assert version is not None
    builder = ContextBuilder()
    xml_samples = []
    class ShellModel(Model):
        async def stream_chat(self, messages, tools=None, metadata=None):
            xml_samples.append(builder._build_environment_context_xml(AgentState(user_message="")))
            async for event in super().stream_chat(messages, tools, metadata):
                yield event
    model = ShellModel([StreamEvent(StreamEventType.TEXT_CHUNK, "complete"), done()])
    runtime = AgentRuntime(metrics_file=tmp_path / "runtime/metrics.jsonl")
    artifacts = ArtifactStore(storage_dir=tmp_path / "artifacts")
    config = AppConfig(llm=model._settings, feature_flags=FeatureFlags({"powershell_shell_version": True}))
    owner = RunContext(agent_runtime=runtime, model_execution=ModelExecutionSnapshot.capture(config, model))
    session = create_session(session_id="shell-version", workspace_root=tmp_path, context_builder=builder,
        run_context=owner, config=config, llm=model, artifact_store=artifacts, tool_registry=ToolRegistry(),
        permission_context=PermissionContext(mode="bypass"))
    try:
        events = [event async for event in session.query("Identify the selected local environment")]
        assert any(event.type == "done" and event.data["status"] == "completed" for event in events)
        assert f"<shell_version>{version}</shell_version>" in xml_samples[-1]
        assert any(f"<shell_version>{version}</shell_version>" in message.content for message in model.inputs)
        disabled = replace(config, feature_flags=FeatureFlags({"powershell_shell_version": False}))
        owner.model_execution = ModelExecutionSnapshot.capture(disabled, model)
        _ = [event async for event in session.query("Observe the same environment with version disabled", config=disabled)]
        assert "<shell_version>" not in xml_samples[-1]
        record_testsuite_property("actual_powershell_version", json.dumps({"shell": selected["name"], "version": version}))
    finally:
        await session.aclose()
        await artifacts.aclose()
        await runtime.aclose()
