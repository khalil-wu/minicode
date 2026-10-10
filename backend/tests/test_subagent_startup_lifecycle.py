from __future__ import annotations

import asyncio
import subprocess
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from backend.agent.run_context import RunContext
from backend.agent.runtime import AgentRuntime
from backend.agent.worktree import create_agent_worktree
from backend.artifact.store import ArtifactStore
from backend.config import AgentSettings, PermissionSettings, TokenBudget
from backend.hooks.manager import HookResult
from backend.llm.base import LLMAdapter, StreamEvent, StreamEventType, ToolCallEvent
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import ToolExecutionContext
from backend.tools.agent_tools import TaskStatusTool, TaskTool
from backend.tools.registry import ToolRegistry
from backend.tools.swarm_tools import SendMessageTool
from backend.tools.toolsets import SESSION_TOOLSET_POLICY_METADATA_KEY, ToolsetPolicy


class _RecordingLLM(LLMAdapter):
    def __init__(self) -> None:
        self.calls = 0

    async def stream_chat(self, messages, tools=None, metadata=None):
        self.calls += 1
        yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="Audit answer")
        yield StreamEvent(type=StreamEventType.DONE)

    async def simple_chat(self, messages):
        return "Audit answer"


@pytest.mark.asyncio
async def test_ordinary_child_refreshes_parent_restrictions_at_its_own_boundary(startup_case, monkeypatch):
    from backend.agent.message import AgentEvent
    from backend.agent.query_engine import QueryEngine
    from backend.permissions.context import PermissionContext

    current = PermissionContext(mode="bypass")
    startup_case.context.run_context.permission_context_provider = lambda: current
    captured = []

    async def submit(self, submission):
        nonlocal current
        provider = submission.runtime.run_context.permission_context_provider
        assert provider().mode == "bypass"
        current = PermissionContext(mode="plan", tool_deny_rules=["web_fetch"])
        refreshed = provider()
        captured.append(refreshed)
        assert refreshed.mode == "plan" and "web_fetch" in refreshed.tool_deny_rules
        current = PermissionContext(mode="bypass")
        assert provider().mode == "bypass"
        submission.state.reply = "Finished"
        yield AgentEvent.done(status="completed", reason="success")

    monkeypatch.setattr(QueryEngine, "submit", submit)
    _, result = await _launch(startup_case, "foreground")
    assert not result.is_error and len(captured) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments, own_mode", [({}, "bypass"), ({"read_only": True}, "bypass"), ({"mode": "plan"}, "plan")])
async def test_teammate_reapplies_the_live_parent_permission_ceiling(startup_case, monkeypatch, arguments, own_mode):
    from backend.agent.message import AgentEvent
    from backend.agent.query_engine import QueryEngine
    from backend.permissions.context import PermissionContext
    from backend.tools.base import PermissionLevel

    current = PermissionContext(mode="bypass")
    startup_case.context.run_context.permission_context_provider = lambda: current
    captured = []

    async def submit(self, submission):
        nonlocal current
        run = submission.runtime.run_context
        provider = run.permission_context_provider
        assert provider().mode == own_mode
        current = PermissionContext(
            mode="plan", tool_deny_rules=["web_fetch"],
            session_overrides={"read_file": PermissionLevel.ALWAYS_DENY},
            filesystem_constraints={"deny_read": ["private/**"]}, allow_unsandboxed_commands=False,
        )
        refreshed = provider()
        assert refreshed.mode == "plan"
        assert "web_fetch" in refreshed.tool_deny_rules
        assert refreshed.session_overrides["read_file"] == PermissionLevel.ALWAYS_DENY
        assert refreshed.filesystem_constraints["deny_read"] == ["private/**"]
        assert not refreshed.allow_unsandboxed_commands
        current = PermissionContext(mode="confirm")
        await run.permission_mode_setter("bypass")
        applied_mode = "confirm"
        assert provider().mode == applied_mode
        assert "web_fetch" not in provider().tool_deny_rules
        record = startup_case.runtime.get_subagent(submission.runtime.metadata["agent_id"])
        assert record.permission_mode == applied_mode
        captured.append(refreshed)
        submission.state.reply = "Finished"
        yield AgentEvent.done(status="completed", reason="success")

    monkeypatch.setattr(QueryEngine, "submit", submit)
    child_id, result = await _launch(startup_case, "teammate", **arguments)
    assert not result.is_error
    assert await startup_case.runtime.wait_for_subagent(child_id, 3)
    assert len(captured) == 1


@pytest.mark.asyncio
async def test_continuation_metrics_sum_query_turns_once(startup_case, monkeypatch):
    from backend.agent.message import AgentEvent
    from backend.agent.query_engine import QueryEngine
    from backend.llm.base import ToolCallEvent, UsageInfo
    from backend.tools import agent_tools

    run_ids = []
    agent_ids = []
    turns = [UsageInfo(input_tokens=100, output_tokens=20, cost_usd=0.1), UsageInfo(input_tokens=80, output_tokens=10, cost_usd=0.2)]

    async def submit(self, submission):
        index = len(run_ids)
        run_ids.append(submission.runtime.metadata["run_id"])
        agent_ids.append(submission.runtime.metadata["agent_id"])
        submission.runtime.run_context.llm_turn_context = SimpleNamespace(usage=turns[index])
        submission.state.reply = f"Turn {index + 1} answer"
        submission.state.iterations = index + 1
        submission.state.tool_calls = [ToolCallEvent(id=f"tool-{index}", name="read_file", arguments={})]
        yield AgentEvent.done(status="completed", reason="success")

    monkeypatch.setattr(QueryEngine, "submit", submit)
    monkeypatch.setattr(agent_tools._SubagentLifecycleOwner, "after_subagent_stop", AsyncMock(side_effect=[SimpleNamespace(action="continue", prompt="Continue the review"), SimpleNamespace(action="terminal")]))
    child_id, result = await _launch(startup_case, "foreground")
    assert not result.is_error
    assert len(set(run_ids)) == 2
    assert agent_ids == [child_id, child_id]
    stored = startup_case.runtime._subagent_results[child_id]
    assert stored.iterations == 3
    assert stored.tool_call_count == 2
    assert stored.usage["input_tokens"] == 180
    assert stored.usage["output_tokens"] == 30
    assert stored.usage["cost_usd"] == pytest.approx(0.3)


@pytest.fixture
def startup_case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime = AgentRuntime(
        metrics_file=tmp_path / "metrics.jsonl",
        swarm_store_dir=tmp_path / "swarm",
        enable_lease_heartbeat=False,
    )
    runtime.start_run(run_id="parent", conversation_id="conversation")
    runtime.create_swarm_team(
        team_name="audit", conversation_id="conversation", created_by="parent"
    )
    hooks = SimpleNamespace(
        has_hooks=lambda event: False,
        bind_runtime=lambda **kwargs: None,
        run_task_created=AsyncMock(return_value=HookResult()),
        run_subagent_start=AsyncMock(return_value=HookResult()),
        run_subagent_stop=AsyncMock(return_value=HookResult()),
        run_pre_tool=AsyncMock(return_value=HookResult()),
        run_post_tool=AsyncMock(return_value=HookResult()),
        run_post_tool_failure=AsyncMock(return_value=HookResult()),
        run_permission_denied=AsyncMock(return_value=HookResult()),
        run_permission_request=AsyncMock(return_value=HookResult()),
        run_teammate_idle=AsyncMock(return_value=HookResult(
            prevent_continuation=True, stop_reason="audit finished"
        )),
    )
    monkeypatch.setattr(
        "backend.hooks.manager.load_hook_manager_for_workspace",
        lambda *args, **kwargs: hooks,
    )
    monkeypatch.setattr(
        "backend.hooks.manager.register_hook_manager_for_session",
        lambda *args, **kwargs: None,
    )
    model = _RecordingLLM()
    registry = ToolRegistry()
    checker = PermissionChecker(PermissionSettings(), workspace_root=workspace)
    tool = TaskTool(
        llm_provider=model,
        tool_registry_provider=registry,
        artifact_store=ArtifactStore(storage_dir=tmp_path / "artifacts"),
        permission_checker_provider=checker,
        agent_settings_provider=AgentSettings(max_iterations=2),
        token_budget_provider=TokenBudget(),
    )
    registry.register(tool)
    events = []
    workers = []

    async def emit(event_type, payload):
        events.append((event_type, payload))
        if event_type == "subagent.start":
            worker = runtime._subagent_tasks.get(payload["subagent_id"])
            if worker is not None:
                workers.append(worker)

    context = ToolExecutionContext(
        permission=checker.build_context(mode="bypass"),
        workspace_root=workspace,
        session_id="session",
        conversation_id="conversation",
        task_id="parent-task",
        emit_event=emit,
        metadata={"run_id": "parent", "_tool_registry": registry},
        run_context=RunContext(agent_runtime=runtime, hook_manager=hooks),
    )
    yield SimpleNamespace(
        runtime=runtime, tool=tool, context=context, hooks=hooks,
        model=model, events=events, workers=workers, workspace=workspace,
    )
    runtime.close(release_lease=True)


async def _launch(case, delivery: str, **arguments):
    launch = await case.tool.execute({
        "description": "Audit startup",
        "prompt": "Answer the audit question.",
        **({"run_in_background": True} if delivery == "background" else {}),
        **({"name": "alice", "team_name": "audit"} if delivery == "teammate" else {}),
        **arguments,
    }, context=case.context)
    return launch.runtime_metadata["subagent_id"], launch


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery, isolated, projectless", [("foreground", False, False), ("background", False, False),
    ("teammate", False, False), ("background", True, False), ("foreground", False, True)])
async def test_long_child_results_are_readable_and_deleted_in_the_parent_scope(startup_case, delivery, isolated, projectless):
    from backend.tools.agent_artifact_tools import ReadArtifactTool
    from backend.tools.base import MAX_TOOL_RESULT_BYTES

    case = startup_case
    if projectless:
        case.context = replace(case.context, workspace_root=None)
    if isolated:
        _prepare_git(case.workspace)
    raw = "delegated evidence\n" + "evidence\n" * (MAX_TOOL_RESULT_BYTES // 8) + "result-tail"

    class LongResultModel(_RecordingLLM):
        async def stream_chat(self, messages, tools=None, metadata=None):
            yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content=raw, phase="final_answer")
            yield StreamEvent(type=StreamEventType.DONE, finish_reason="stop")

    case.tool._llm_provider = LongResultModel()
    child_id, launched = await _launch(case, delivery, **({"isolation": "worktree"} if isolated else {}))
    if delivery != "foreground":
        assert await case.runtime.wait_for_subagent(child_id, timeout=5)
        await asyncio.gather(*case.workers)
    else:
        assert not launched.is_error, launched.content
    snapshot = case.runtime.get_subagent_snapshot(child_id, include_result=True)
    assert snapshot["status"] == "completed", snapshot
    artifact_id = snapshot["result"]["artifact_id"]
    store = case.tool._artifact_store
    result = await ReadArtifactTool(store).execute({"artifact_id": artifact_id, "offset": len(raw.splitlines()), "limit": 1}, context=case.context)
    assert not result.is_error and "result-tail" in result.content
    assert store.get(artifact_id, conversation_id="other-conversation", workspace_root=case.workspace) is None
    assert store.delete_for_conversation(case.context.conversation_id) == 1
    assert store.get(artifact_id) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery", ["foreground", "background", "teammate"])
@pytest.mark.parametrize("agent_type", ["general-purpose", "explore", "plan"])
@pytest.mark.parametrize("denied_web", [False, True])
async def test_read_only_weather_child_keeps_full_access_approval_and_write_boundary(
    startup_case, delivery, agent_type, denied_web,
):
    import httpx
    from backend.tools.web_tools import WebFetchTool
    from backend.tools.write_file import WriteFileTool

    requests = []

    async def serve_weather(reader, writer):
        requests.append(await reader.readuntil(b"\r\n\r\n"))
        body = (
            b"<html><body><h1>Weather forecast</h1><p>Published 2026-10-08 at 18:00. "
            b"Hangzhou: sunny today, low 15 C and high 27 C, northeast wind below level 3. "
            b"Tomorrow will be sunny with temperatures from 17 C to 27 C. "
            b"The following day will be sunny, low 18 C and high 26 C. "
            b"No precipitation is forecast for these three dates.</p></body></html>"
        )
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
            + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
            + body
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(serve_weather, "127.0.0.1", 0)
    url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/weather"

    class WeatherModel(_RecordingLLM):
        async def stream_chat(self, messages, tools=None, metadata=None):
            self.calls += 1
            if self.calls == 1:
                yield StreamEvent(type=StreamEventType.TOOL_CALL, tool_calls=[
                    ToolCallEvent(id="weather-read", name="web_fetch", arguments={"url": url, "prompt": "Extract the forecast."}),
                    ToolCallEvent(id="readonly-write", name="write_file", arguments={"file_path": "weather.txt", "content": "Unauthorized write"}),
                ])
                yield StreamEvent(type=StreamEventType.DONE, finish_reason="tool_calls")
                return
            yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="Read-only weather research completed.")
            yield StreamEvent(type=StreamEventType.DONE)

    model = WeatherModel()
    startup_case.tool._llm_provider = model
    registry = startup_case.context.tool_registry
    web = WebFetchTool(startup_case.tool._artifact_store)
    registry.register(web)
    registry.register(WriteFileTool())
    if denied_web:
        startup_case.context.permission = replace(
            startup_case.context.permission, tool_deny_rules=["web_fetch"],
        )
    startup_case.context.approval_handler = AsyncMock(return_value={"action": "reject"})
    try:
        async with httpx.AsyncClient(trust_env=False) as client:
            web._unrestricted_client = client
            child_id, result = await _launch(
                startup_case, delivery, agent_type=agent_type, read_only=True,
            )
            if delivery in {"background", "teammate"}:
                assert await startup_case.runtime.wait_for_subagent(child_id, 3)
            assert not result.is_error
        startup_case.context.approval_handler.assert_not_awaited()
        child = startup_case.runtime.get_subagent(child_id)
        assert child.read_only and child.permission_mode == "bypass"
        assert model.calls == 2
        assert bool(requests) is not denied_web
        assert not (startup_case.workspace / "weather.txt").exists()
        journal = startup_case.runtime.execution_journal(child_id)
        results = {
            event.payload["tool_call_id"]: event.payload
            for event in journal.read_events() if event.event_type == "tool_result"
        }
        assert results["readonly-write"]["status"] == "blocked"
        assert "marked read_only" in results["readonly-write"]["content"]
        if denied_web:
            assert results["weather-read"]["status"] == "blocked"
        else:
            assert results["weather-read"]["status"] == "success"
            assert results["weather-read"]["source_url"] == url
            assert results["weather-read"]["extraction_status"] == "ok"
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery", ["foreground", "background"])
@pytest.mark.parametrize("restriction", ["none", "parent-deny", "parent-read-only"])
async def test_parallel_weather_without_optional_scopes_inherits_real_parent_permissions(startup_case, delivery, restriction):
    import httpx
    from backend.tools.web_tools import WebFetchTool
    from backend.tools.write_file import WriteFileTool

    requests = []
    all_requested = asyncio.Event()

    async def serve_weather(reader, writer):
        request = await reader.readuntil(b"\r\n\r\n")
        city = request.split(b" ")[1].decode().strip("/")
        requests.append(city)
        if len(requests) == 3:
            all_requested.set()
        try:
            await asyncio.wait_for(all_requested.wait(), 5)
            body = (f"Weather bulletin WEATHER_{city}: published 2026-10-08. "
                    "Today's observed temperature is 22 C. The forecast is sunny with a low of 16 C "
                    "and a high of 26 C. Wind is from the northeast at 8 km per hour. "
                    "The following morning is expected to be clear with no precipitation.").encode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain; charset=utf-8\r\n"
                         + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(serve_weather, "127.0.0.1", 0)
    cities = {"beijing": "北京", "shanghai": "上海", "guangzhou": "广州"}
    urls = {city: f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/{city}" for city in cities}

    class ParallelWeatherModel(_RecordingLLM):
        def __init__(self):
            super().__init__()
            self.started = set()

        async def side_query(self, messages, **kwargs):
            return messages[0].content

        async def stream_chat(self, messages, tools=None, metadata=None):
            self.calls += 1
            text = "\n".join(str(message.content) for message in messages)
            city = next(city for city, url in urls.items() if url in text)
            if city not in self.started:
                self.started.add(city)
                calls = [ToolCallEvent(id=f"weather-{city}", name="web_fetch", arguments={
                    "url": urls[city], "prompt": "Extract the bulletin.",
                })]
                if restriction != "none":
                    calls.append(ToolCallEvent(id=f"write-{city}", name="write_file", arguments={
                        "file_path": f"{city}.txt", "content": "This write must be blocked by the inherited restriction.",
                    }))
                yield StreamEvent(type=StreamEventType.TOOL_CALL, tool_calls=calls)
                yield StreamEvent(type=StreamEventType.DONE, finish_reason="tool_calls")
                return
            yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content=f"Weather bulletin {city} reviewed.")
            yield StreamEvent(type=StreamEventType.DONE)

    model = ParallelWeatherModel()
    startup_case.tool._llm_provider = model
    web = WebFetchTool(startup_case.tool._artifact_store)
    registry = startup_case.context.tool_registry
    registry.register(web)
    registry.register(WriteFileTool())
    if restriction == "parent-deny":
        startup_case.context.permission = replace(startup_case.context.permission, tool_deny_rules=["write_file"])
    elif restriction == "parent-read-only":
        startup_case.context.metadata["read_only"] = True
    startup_case.context.approval_handler = AsyncMock(return_value={"action": "reject"})
    try:
        async with httpx.AsyncClient(trust_env=False) as client:
            web._unrestricted_client = client
            result = await startup_case.tool.execute({
                "parallel_tasks": [{"description": f"查询{label}今日天气", "prompt": f"Read the bulletin at {urls[city]} and report it."}
                                   for city, label in cities.items()],
                "run_in_background": delivery == "background",
            }, startup_case.context)
            assert not result.is_error, result.content
            if delivery == "background":
                await asyncio.gather(*list(startup_case.runtime._subagent_tasks.values()))
        child_ids = [payload["subagent_id"] for kind, payload in startup_case.events if kind == "subagent.start"]
        assert len(child_ids) == 3
        assert set(requests) == set(cities)
        assert model.calls == 6
        startup_case.context.approval_handler.assert_not_awaited()
        for child_id in child_ids:
            child = startup_case.runtime.get_subagent(child_id)
            assert child.permission_mode == "bypass"
            # This record stores the child's declaration. The inherited
            # restriction is checked against actual tool results below.
            assert child.read_only is False
            assert child.write_scope == []
            journal = startup_case.runtime.execution_journal(child_id)
            results = [event.payload for event in journal.read_events() if event.event_type == "tool_result"]
            reads = [item for item in results if item.get("tool_name") == "web_fetch"]
            assert len(reads) == 1 and reads[0]["status"] == "success"
            assert reads[0]["source_url"] in urls.values()
            if restriction != "none":
                writes = [item for item in results if item.get("tool_name") == "write_file"]
                assert len(writes) == 1 and writes[0]["status"] == "blocked"
                if restriction == "parent-read-only":
                    assert "marked read_only" in writes[0]["content"]
        assert not any((startup_case.workspace / f"{city}.txt").exists() for city in cities)
    finally:
        server.close()
        await server.wait_closed()
        startup_case.tool._artifact_store.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("child_hosted", [False, True])
async def test_child_search_schema_uses_its_actual_model_without_inheriting_parent_source_absence(startup_case, monkeypatch, child_hosted):
    import backend.vault.store as vault_module
    from backend.agent.tool_schema_derivation import effective_toolset_policy
    from backend.config import AppConfig, LLMSettings
    from backend.tools.subagent_support import _SubagentLLMResolution
    from backend.tools.toolsets import ACTIVE_TOOLSET_POLICY_METADATA_KEY
    from backend.tools.web_tools import WebSearchTool

    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setattr(vault_module, "VAULT_FILE", startup_case.workspace.parent / "vault.json")
    parent = startup_case.model
    parent.supports_hosted_web_search = lambda: not child_hosted
    search = WebSearchTool(parent)
    registry = startup_case.context.tool_registry
    registry.register(search)
    parent_policy = effective_toolset_policy(
        base_policy=ToolsetPolicy.default(), tool_registry=registry, disabled_tools=set(),
        requires_explicit_workspace=False, workspace_root=startup_case.workspace,
        permission_mode="bypass", hosted_web_search=not child_hosted,
    )
    startup_case.context.metadata[ACTIVE_TOOLSET_POLICY_METADATA_KEY] = parent_policy
    startup_case.context.metadata[SESSION_TOOLSET_POLICY_METADATA_KEY] = ToolsetPolicy.default()

    class ChildModel(_RecordingLLM):
        def supports_hosted_web_search(self):
            return child_hosted

        async def stream_chat(self, messages, tools=None, metadata=None):
            names = {item["function"]["name"] for item in tools or []}
            assert ("web_search" in names) is child_hosted
            self.calls += 1
            yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="Verified the child's search capability.")
            yield StreamEvent(type=StreamEventType.DONE)

    child = ChildModel()
    config = AppConfig(llm=LLMSettings(api_key="", model="child-search-model"), agent=AgentSettings(max_iterations=2))
    resolution = _SubagentLLMResolution(llm=child, config=config, provider="custom", model="child-search-model", effort="off")
    result = await startup_case.tool._run_single_subtask_impl(
        description="Child search capability", prompt="Inspect the search surface.", agent_type="general-purpose",
        context=startup_case.context, llm_resolution=resolution,
    )
    assert not result.is_error, result.content
    assert child.calls == 1 and parent.calls == 0
    assert parent_policy.is_available(registry.get_tool_spec("web_search")) is not child_hosted
    assert search._llm_provider is parent


@pytest.mark.asyncio
async def test_parallel_children_keep_the_parent_lifecycle_registry_binding(startup_case):
    from backend.extensions.runtime import ExtensionRunner

    registry = startup_case.context.tool_registry
    lifecycle = ExtensionRunner(cwd=startup_case.workspace)
    lifecycle.bind_tool_registry(registry)
    startup_case.context.run_context.lifecycle_runtime = lifecycle

    launched = await asyncio.gather(*(
        _launch(startup_case, "foreground", description=f"Parallel child {index}")
        for index in range(3)
    ))

    assert all(not result.is_error for _, result in launched)
    assert startup_case.model.calls == 3
    assert lifecycle._tool_registry is registry
    assert registry.get_tool("task") is startup_case.tool
    assert all(startup_case.runtime.get_subagent(child_id).status == "completed" for child_id, _ in launched)


@pytest.mark.asyncio
async def test_independent_registry_still_requires_a_new_lifecycle_generation(startup_case, caplog):
    from backend.agent.loop import AgentLoopSessionContext
    from backend.agent.query_engine import AgentSession, QueryEngine, QuerySubmission
    from backend.extensions.runtime import ExtensionRunner

    parent_registry = startup_case.context.tool_registry
    lifecycle = ExtensionRunner(cwd=startup_case.workspace)
    lifecycle.bind_tool_registry(parent_registry)
    session = AgentSession(
        llm=startup_case.model, tool_registry=ToolRegistry(),
        artifact_store=startup_case.tool._artifact_store,
        permission_checker=PermissionChecker(PermissionSettings(), workspace_root=startup_case.workspace),
        agent_settings=AgentSettings(max_iterations=1), token_budget=TokenBudget(),
        lifecycle_runtime=lifecycle,
    )
    events = [event async for event in QueryEngine().submit(QuerySubmission(
        user_message="Check the replacement registry", session=session,
        runtime=AgentLoopSessionContext(workspace_root=startup_case.workspace,
            run_context=RunContext(agent_runtime=startup_case.runtime, hook_manager=startup_case.hooks)),
    ))]

    assert startup_case.model.calls == 0
    assert "already bound to a different ToolRegistry" in caplog.text
    assert events[-1].data["status"] == "failed"
    assert lifecycle._tool_registry is parent_registry
    await session.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("rebuild_runtime", [False, True])
async def test_team_owner_survives_next_turn_and_runtime_rebuild(startup_case, rebuild_runtime):
    from backend.tools.swarm_tools import TeamCreateTool, TeamDeleteTool, TeamListTool

    runtime = startup_case.runtime
    runtime.complete_run("parent")
    if rebuild_runtime:
        runtime.close()
        runtime = AgentRuntime(
            metrics_file=startup_case.workspace.parent / "metrics.jsonl",
            swarm_store_dir=startup_case.workspace.parent / "swarm", enable_lease_heartbeat=False,
        )
        startup_case.context.run_context.agent_runtime = runtime
    owned_workers = []
    original_emit = startup_case.context.emit_event

    async def capture_owned_worker(event_type, payload):
        if event_type == "subagent.start":
            owned_workers.append(runtime._subagent_tasks[payload["subagent_id"]])
        await original_emit(event_type, payload)

    startup_case.context.emit_event = capture_owned_worker
    try:
        runtime.start_run(run_id="next-turn", conversation_id="conversation")
        startup_case.context.metadata["run_id"] = "next-turn"
        child_id, result = await _launch(startup_case, "teammate", team_name="")
        assert not result.is_error
        assert child_id == "alice@audit"
        assert runtime.get_subagent(child_id).team_name == "audit"
        await asyncio.gather(*owned_workers)
        assert await runtime.wait_for_subagent(child_id, 0)
        assert runtime.get_subagent(child_id).status == "completed"
        team = runtime.list_swarm_teams(conversation_id="conversation")[0]
        assert team.created_by == "parent"
        duplicate = await TeamCreateTool().execute({"team_name": "second"}, startup_case.context)
        assert duplicate.is_error
        listing = await TeamListTool().execute({"team_name": "audit"}, startup_case.context)
        assert not listing.is_error and "audit" in listing.content
        deleted = await TeamDeleteTool().execute({"team_name": "audit"}, startup_case.context)
        assert not deleted.is_error
    finally:
        for worker in owned_workers:
            if not worker.done():
                worker.cancel()
        await asyncio.gather(*owned_workers, return_exceptions=True)
        if rebuild_runtime:
            runtime.close()


@pytest.mark.asyncio
async def test_implicit_team_selection_rejects_ambiguous_history(startup_case):
    startup_case.runtime.create_swarm_team(
        team_name="legacy-other", conversation_id="conversation", created_by="earlier-turn",
    )
    result = await startup_case.tool.execute({
        "name": "alice", "description": "Audit startup", "prompt": "Answer the audit question.",
    }, context=startup_case.context)
    assert result.is_error and "team_name" in result.content
    assert not startup_case.workers
    child_id, explicit = await _launch(startup_case, "teammate", team_name="audit")
    assert not explicit.is_error and child_id == "alice@audit"
    assert await startup_case.runtime.wait_for_subagent(child_id, 3)


@pytest.mark.asyncio
async def test_child_text_stream_uses_deltas_between_durable_snapshots(startup_case):
    import json

    class BurstModel(_RecordingLLM):
        async def stream_chat(self, messages, tools=None, metadata=None):
            for _ in range(80):
                yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="观察😀并验证。")
            yield StreamEvent(type=StreamEventType.DONE, finish_reason="stop")

    startup_case.tool._llm_provider = BurstModel()
    child_id, result = await _launch(startup_case, "foreground")
    assert not result.is_error, result.content
    progress = [data for kind, data in startup_case.events if kind == "subagent.progress"]
    deltas = [data["transcript_delta"] for data in progress if "transcript_delta" in data]
    assert len(deltas) >= 70
    assert all("transcript_snapshot" not in data for data in progress if "transcript_delta" in data)
    assert len([data for data in progress if "transcript_snapshot" in data]) < 8
    assert deltas[0]["offset"] == len("观察😀并验证。")
    assert deltas[-1]["offset"] == 79 * len("观察😀并验证。")
    terminal = next(data for kind, data in reversed(startup_case.events) if kind == "subagent.done")
    parent_id = startup_case.runtime.get_subagent(child_id).parent_run_id
    assert parent_id == "parent"
    assert all(data["parent_run_id"] == parent_id for data in progress)
    assert terminal["parent_run_id"] == parent_id
    record = startup_case.runtime.get_subagent(child_id)
    execution = {"model": record.model, "provider": record.provider, "reasoning_effort": record.reasoning_effort}
    assert all({key: data[key] for key in execution} == execution for data in progress)
    assert {key: terminal[key] for key in execution} == execution
    assert terminal["transcript_snapshot"]["messages"][-1]["content"] == "观察😀并验证。" * 80
    assert len(json.dumps(startup_case.events, ensure_ascii=False)) < 200_000


@pytest.mark.asyncio
async def test_child_headers_follow_resolved_and_captured_step_model_instead_of_parent_selection(startup_case, monkeypatch):
    from dataclasses import replace
    from backend.agent.message import AgentEvent
    from backend.agent.model_execution import ModelExecutionSnapshot
    from backend.agent.query_engine import QueryEngine
    from backend.config import AppConfig, LLMSettings
    from backend.tools.subagent_support import _SubagentLLMResolution

    config = AppConfig(llm=LLMSettings(api_key="", provider="openai", model="resolved-child", reasoning_effort="medium"))
    resolution = _SubagentLLMResolution(
        llm=startup_case.model, config=config, provider="openai", model="resolved-child", effort="medium",
    )
    startup_case.context.model_execution = ModelExecutionSnapshot(
        config=config, llm=startup_case.model, provider="parent-provider", model="new-parent-selection", thinking_level="low",
    )

    async def submit(self, submission):
        context = submission.runtime.run_context
        assert context.model_execution.model == "resolved-child"
        captured = replace(context.model_execution, model="actual-child-step", thinking_level="high")
        context.model_execution = context.active_model_execution = captured
        yield AgentEvent(type="tool_call", data={"id": "fixture-call", "tool_name": "read_file", "arguments": {}})
        submission.state.reply = "Finished"
        yield AgentEvent.done(status="completed", reason="success")

    monkeypatch.setattr(QueryEngine, "submit", submit)
    result = await startup_case.tool._run_single_subtask_impl(
        description="Metadata fixture", prompt="Inspect metadata", agent_type="general-purpose",
        context=startup_case.context, subagent_id="metadata-child", llm_resolution=resolution,
    )
    assert not result.is_error
    start = next(data for kind, data in startup_case.events if kind == "subagent.start")
    progress = next(data for kind, data in startup_case.events if kind == "subagent.progress")
    terminal = next(data for kind, data in reversed(startup_case.events) if kind == "subagent.done")
    assert (start["model"], start["provider"], start["reasoning_effort"]) == ("resolved-child", "openai", "medium")
    assert (progress["model"], progress["provider"], progress["reasoning_effort"]) == ("actual-child-step", "openai", "high")
    assert (terminal["model"], terminal["provider"], terminal["reasoning_effort"]) == ("actual-child-step", "openai", "high")
    record = startup_case.runtime.get_subagent("metadata-child")
    assert record.model == record.resume_config["model"] == "actual-child-step"
    assert record.reasoning_effort == record.resume_config["reasoning_effort"] == "high"
    assert startup_case.context.model_execution.model == "new-parent-selection"


def _assert_terminal(case, subagent_id: str, status: str, epoch: int = 1):
    record = case.runtime.get_subagent(subagent_id)
    result = case.runtime._subagent_results[subagent_id]
    assert record.status == result.status == status
    assert record.mailbox_epoch == result.mailbox_epoch == epoch
    assert case.runtime._swarm_store.get_subagent(subagent_id) == record.to_dict()
    assert case.runtime._swarm_store.get_subagent_result(subagent_id) == result.to_dict()
    assert [event_type for event_type, _payload in case.events] == [
        "subagent.start", "subagent.done"
    ]
    done = case.events[-1][1]
    assert done["status"] == done["record"]["status"] == done["result"]["status"] == status
    assert done["agent_path"] == record.agent_path
    assert done["mailbox_epoch"] == epoch
    assert done["result"]["iterations"] == 0
    assert done["result"]["tool_call_count"] == 0
    return record, result


@pytest.mark.parametrize("delivery", ["foreground", "background", "teammate", "resume", "teammate_resume"])
@pytest.mark.parametrize("fault", ["veto", "hook", "journal_open", "journal_read", "context"])
def test_startup_failures_finish_the_owned_incarnation(
    startup_case, monkeypatch: pytest.MonkeyPatch, delivery: str, fault: str
):
    case = startup_case

    async def run():
        subagent_id = ""
        resuming = delivery in {"resume", "teammate_resume"}
        if resuming:
            subagent_id, _first = await _launch(
                case, "teammate" if delivery == "teammate_resume" else "foreground"
            )
            assert await case.runtime.wait_for_subagent(subagent_id, timeout=5)
            await asyncio.gather(*case.workers)
            assert case.runtime.get_subagent(subagent_id).status == "completed"
            case.events.clear()
            case.workers.clear()
        previous_calls = case.model.calls
        expected_error = "audit startup failure"
        if fault == "veto":
            case.hooks.run_subagent_start.return_value = HookResult(
                blocked=True, message=expected_error
            )
        elif fault == "hook":
            case.hooks.run_subagent_start.side_effect = RuntimeError(expected_error)
        elif fault == "journal_open":
            monkeypatch.setattr(case.runtime, "execution_journal", Mock(
                side_effect=OSError(expected_error)
            ))
            expected_error = "journal could not be opened"
        elif fault == "journal_read":
            monkeypatch.setattr("backend.tools.agent_tools.ExecutionJournal.read_events", Mock(
                side_effect=OSError(expected_error)
            ))
            expected_error = "journal could not be opened"
        else:
            monkeypatch.setattr(case.tool, "_build_subagent_context_builder", Mock(
                side_effect=RuntimeError(expected_error)
            ))
        if resuming:
            await case.tool.resume_background_subtask(
                subagent_id=subagent_id, prompt="Continue the audit.", context=case.context
            )
        else:
            subagent_id, launch = await _launch(case, delivery)
            if delivery == "foreground":
                assert launch.status == "failed"
                assert launch.is_error
                assert expected_error in launch.content
        assert await case.runtime.wait_for_subagent(subagent_id, timeout=5)
        await asyncio.gather(*case.workers)
        await asyncio.sleep(0)
        record, result = _assert_terminal(
            case, subagent_id, "failed", epoch=2 if resuming else 1
        )
        assert expected_error in result.error
        assert case.model.calls == previous_calls
        assert not case.runtime._subagent_tasks
        assert not case.runtime._subagent_slot_reservations
        status = await TaskStatusTool().execute(
            {"subagent_id": subagent_id}, context=case.context
        )
        assert status.status == record.status
        assert expected_error in status.content

    asyncio.run(run())


@pytest.mark.parametrize("delivery", ["foreground", "background", "teammate", "resume"])
def test_cancellation_during_start_hook_finishes_the_owned_incarnation(startup_case, delivery):
    case = startup_case

    async def run():
        entered = asyncio.Event()

        async def hold_start(**kwargs):
            entered.set()
            await asyncio.Event().wait()

        if delivery == "resume":
            subagent_id, first = await _launch(case, "foreground")
            assert first.status == "completed"
            case.events.clear()
        previous_calls = case.model.calls
        case.hooks.run_subagent_start.side_effect = hold_start
        if delivery == "foreground":
            worker = asyncio.create_task(_launch(case, delivery))
        elif delivery == "resume":
            await case.tool.resume_background_subtask(
                subagent_id=subagent_id, prompt="Continue the audit.", context=case.context
            )
            worker = case.workers[-1]
        else:
            subagent_id, _launch_result = await _launch(case, delivery)
            worker = case.workers[-1]
        await asyncio.wait_for(entered.wait(), timeout=5)
        subagent_id = case.events[0][1]["subagent_id"]
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker
        assert await case.runtime.wait_for_subagent(subagent_id, timeout=1)
        _assert_terminal(case, subagent_id, "cancelled", epoch=2 if delivery == "resume" else 1)
        assert case.model.calls == previous_calls

    asyncio.run(run())


@pytest.mark.parametrize("cancelled", [False, True], ids=["failure", "cancellation"])
def test_late_startup_exit_does_not_overwrite_a_new_incarnation(startup_case, cancelled):
    case = startup_case

    async def run():
        entered = asyncio.Event()
        release = asyncio.Event()

        async def hold_start(**kwargs):
            entered.set()
            await release.wait()
            raise RuntimeError("old startup failure")

        case.hooks.run_subagent_start.side_effect = hold_start
        subagent_id, _launch_result = await _launch(case, "background")
        await asyncio.wait_for(entered.wait(), timeout=5)
        worker = case.workers[-1]
        old = case.runtime.get_subagent(subagent_id)
        case.runtime.complete_subagent(
            subagent_id, "cancelled", agent_path=old.agent_path, mailbox_epoch=old.mailbox_epoch
        )
        replacement = case.runtime.start_subagent(
            subagent_id=subagent_id, parent_run_id="parent", agent_type=old.agent_type,
            session_id="session", background=True,
        )
        if cancelled:
            worker.cancel()
        else:
            release.set()
        await asyncio.gather(worker, return_exceptions=True)
        assert case.runtime.get_subagent(subagent_id).to_dict() == replacement.to_dict()
        assert case.runtime._subagent_results.get(subagent_id) is None
        assert case.runtime._swarm_store.get_subagent_result(subagent_id) is None
        assert [event_type for event_type, _payload in case.events] == ["subagent.start"]
        assert case.model.calls == 0

    asyncio.run(run())


def _prepare_git(workspace: Path):
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    (workspace / "seed.txt").write_text("audit fixture\n", encoding="utf-8")
    subprocess.run(["git", "add", "seed.txt"], cwd=workspace, check=True)
    subprocess.run([
        "git", "-c", "user.name=Audit Fixture", "-c", "user.email=audit@example.invalid",
        "commit", "-qm", "fixture",
    ], cwd=workspace, check=True)


@pytest.mark.parametrize("changed", [False, True], ids=["clean", "changed"])
@pytest.mark.parametrize("cancelled", [False, True], ids=["veto", "cancellation"])
def test_startup_exit_cleans_only_its_owned_worktree(startup_case, changed, cancelled):
    case = startup_case
    _prepare_git(case.workspace)

    async def run():
        entered = asyncio.Event()
        worktree_path = None

        async def stop_start(**kwargs):
            nonlocal worktree_path
            record = case.runtime.get_subagent(kwargs["subagent_id"])
            worktree_path = Path(record.resume_config["worktree_path"])
            if changed:
                (worktree_path / "user-change.txt").write_text("keep this\n", encoding="utf-8")
            entered.set()
            if cancelled:
                await asyncio.Event().wait()
            return HookResult(blocked=True, message="audit veto")

        case.hooks.run_subagent_start.side_effect = stop_start
        subagent_id, _launch_result = await _launch(case, "background", isolation="worktree")
        await asyncio.wait_for(entered.wait(), timeout=5)
        if cancelled:
            case.workers[-1].cancel()
        await asyncio.gather(*case.workers, return_exceptions=True)
        assert await case.runtime.wait_for_subagent(subagent_id, timeout=1)
        record, _result = _assert_terminal(case, subagent_id, "cancelled" if cancelled else "failed")
        assert worktree_path.exists() is changed
        resource, = record.cleanup_resources
        assert resource["resource_id"] == str(worktree_path)
        assert resource["state"] == ("retained" if changed else "released")
        if changed:
            assert (worktree_path / "user-change.txt").read_text(encoding="utf-8") == "keep this\n"

    asyncio.run(run())


@pytest.mark.parametrize("fault", ["team", "worktree", "resource", "resume_config"])
def test_early_setup_failure_publishes_a_terminal_result(startup_case, monkeypatch, fault):
    case = startup_case
    if fault == "team":
        monkeypatch.setattr(case.runtime, "add_swarm_team_member", Mock(return_value=None))
    elif fault == "worktree":
        monkeypatch.setattr("backend.agent.worktree.create_agent_worktree", Mock(
            return_value=(None, "audit worktree unavailable")
        ))
    elif fault == "resource":
        _prepare_git(case.workspace)
        monkeypatch.setattr(case.runtime, "register_subagent_cleanup_resource", Mock(return_value=False))
    else:
        monkeypatch.setattr(case.runtime, "update_subagent_resume_config", Mock(return_value=None))

    async def run():
        subagent_id, _launch_result = await _launch(
            case, "teammate" if fault == "team" else "background",
            **({"isolation": "worktree"} if fault in {"worktree", "resource"} else {}),
        )
        assert await case.runtime.wait_for_subagent(subagent_id, timeout=5)
        await asyncio.gather(*case.workers)
        _assert_terminal(case, subagent_id, "failed")
        assert not (case.workspace / ".minicode" / "worktrees" / subagent_id).exists()
        assert case.model.calls == 0

    asyncio.run(run())


def test_cancellation_waits_for_inflight_worktree_acquisition(startup_case, monkeypatch):
    case = startup_case
    _prepare_git(case.workspace)
    release = threading.Event()

    async def run():
        acquired = asyncio.Event()
        loop = asyncio.get_running_loop()
        created = []

        def delayed_create(subagent_id, workspace):
            result = create_agent_worktree(subagent_id, workspace)
            created.append(result[0])
            loop.call_soon_threadsafe(acquired.set)
            release.wait(timeout=5)
            return result

        monkeypatch.setattr("backend.agent.worktree.create_agent_worktree", delayed_create)
        subagent_id, _launch_result = await _launch(case, "background", isolation="worktree")
        try:
            await asyncio.wait_for(acquired.wait(), timeout=5)
            worker = case.workers[-1]
            worker.cancel()
            await asyncio.sleep(0)
            assert not worker.done()
            release.set()
            await asyncio.gather(worker, return_exceptions=True)
            assert await case.runtime.wait_for_subagent(subagent_id, timeout=1)
            record, _result = _assert_terminal(case, subagent_id, "cancelled")
            worktree, = created
            assert not worktree.worktree_path.exists()
            resource, = record.cleanup_resources
            assert resource["resource_id"] == str(worktree.worktree_path)
            assert resource["state"] == "released"
            assert case.model.calls == 0
        finally:
            release.set()

    asyncio.run(run())


@pytest.mark.parametrize("persisted_policy", [False, True], ids=["absent-policy", "explicit-policy"])
def test_named_teammate_resume_keeps_identity_membership_and_permission(startup_case, persisted_policy):
    case = startup_case
    if persisted_policy:
        case.context.metadata[SESSION_TOOLSET_POLICY_METADATA_KEY] = ToolsetPolicy(
            disabled_tools=frozenset({"bash"})
        )

    async def run():
        subagent_id, _first = await _launch(case, "teammate", mode="auto")
        assert await case.runtime.wait_for_subagent(subagent_id, timeout=5)
        await asyncio.gather(*case.workers)
        initial = case.runtime.get_subagent(subagent_id)
        assert initial.status == "completed"
        assert initial.teammate_name == "alice"
        case.runtime._subagent_task_metadata.pop(subagent_id, None)
        sent = await SendMessageTool().execute({
            "recipient": "alice", "message": "Continue the audit with the same identity."
        }, context=case.context)
        assert not sent.is_error, sent.content
        assert await case.runtime.wait_for_subagent(subagent_id, timeout=5)
        await asyncio.gather(*case.workers)
        resumed = case.runtime.get_subagent(subagent_id)
        assert resumed.status == "completed"
        assert resumed.agent_path == initial.agent_path
        assert resumed.mailbox_epoch == initial.mailbox_epoch + 1
        assert resumed.teammate_name == initial.teammate_name
        assert resumed.permission_mode == initial.permission_mode
        assert resumed.resume_config["session_toolset_policy"] == initial.resume_config["session_toolset_policy"]
        team, = case.runtime.list_swarm_teams(conversation_id="conversation", team_name="audit")
        assert [member.id for member in team.members] == [subagent_id]
        assert case.runtime.resolve_subagent_name("alice") == subagent_id
        assert case.model.calls == 2
        assert [payload["status"] for kind, payload in case.events if kind == "subagent.done"] == [
            "completed", "completed"
        ]

    asyncio.run(run())


@pytest.mark.parametrize("policy", [False, [], {"availability_filters": [False]}])
def test_resume_still_rejects_malformed_persisted_policy(startup_case, policy):
    case = startup_case

    async def run():
        subagent_id, initial = await _launch(case, "foreground")
        assert initial.status == "completed"
        record = case.runtime.get_subagent(subagent_id)
        record.resume_config["session_toolset_policy"] = policy
        case.runtime._swarm_store.upsert_subagent(
            record.to_dict(), expected_owner_token=record.runtime_owner_token,
        )
        before = case.runtime.get_subagent(subagent_id).to_dict()
        with pytest.raises(RuntimeError, match="invalid persisted tool capability policy"):
            await case.tool.resume_background_subtask(
                subagent_id=subagent_id, prompt="Continue.", context=case.context
            )
        assert case.runtime.get_subagent(subagent_id).to_dict() == before
        assert case.model.calls == 1

    asyncio.run(run())


def test_resume_snapshot_failure_finishes_the_new_incarnation(startup_case, monkeypatch):
    case = startup_case

    async def run():
        subagent_id, initial = await _launch(case, "foreground")
        assert initial.status == "completed"
        case.events.clear()
        monkeypatch.setattr("backend.agent.context.ContextBuilder.load_snapshot", Mock(
            side_effect=ValueError("audit snapshot could not be loaded")
        ))
        await case.tool.resume_background_subtask(
            subagent_id=subagent_id, prompt="Continue.", context=case.context
        )
        assert await case.runtime.wait_for_subagent(subagent_id, timeout=5)
        await asyncio.gather(*case.workers)
        _record, result = _assert_terminal(case, subagent_id, "failed", epoch=2)
        assert "audit snapshot could not be loaded" in result.error
        assert case.model.calls == 1

    asyncio.run(run())
