"""Real QueryEngine callback admission before native execution and cold recovery."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
import shlex
import sys

import pytest

from backend.agent.context import ContextBuilder
from backend.agent.execution_journal import ExecutionJournal
from backend.agent.loop_session import AgentLoopSessionContext
from backend.agent.query_engine import AgentSession, QueryEngine, QuerySubmission
from backend.agent.run_context import RunContext
from backend.agent.runtime import AgentRuntime
from backend.agent.state import AgentState
from backend.config import AgentSettings, TokenBudget, PermissionSettings
from backend.llm.base import LLMAdapter, StreamEvent, StreamEventType, ToolCallEvent
from backend.permissions.context import PermissionContext
from backend.permissions.checker import PermissionChecker
from backend.tests.test_native_exec_policy_chain import output_command, setup
from backend.tools.code_execution import ToolExecTool, ToolWaitTool


class NativeCommandModel(LLMAdapter):
    def __init__(self, settings, call):
        self._settings, self.call, self.calls = settings, call, 0

    async def simple_chat(self, *args, **kwargs):
        raise AssertionError("This fixture must use the actual streamed harness")

    async def stream_chat(self, messages, tools=None, metadata=None):
        self.calls += 1
        if self.calls == 1:
            yield StreamEvent(type=StreamEventType.TOOL_CALL, tool_calls=[self.call], tool_calls_committed=True)
            yield StreamEvent(type=StreamEventType.DONE, finish_reason="tool_calls")
        else:
            yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="ACTUAL_NATIVE_START_COMPLETE", phase="final_answer")
            yield StreamEvent(type=StreamEventType.DONE, finish_reason="stop")


def shell_path(path: Path) -> str:
    return "'" + path.as_posix().replace("'", "''") + "'"


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["direct", "direct_stream", "code_mode"])
@pytest.mark.parametrize("with_host_callback", [False, True])
async def test_actual_query_command_start_is_durable_before_host_and_without_host_while_result_is_missing(tmp_path, shape, with_host_callback):
    host = setup(tmp_path, approval_policy="never")
    entered, release = host.workspace / "actual-entered.txt", host.workspace / "actual-release.txt"
    command = (f"Set-Content -LiteralPath {shell_path(entered)} -Value 'ACTUAL_NATIVE_EXECUTION'; "
        f"while (-not (Test-Path -LiteralPath {shell_path(release)})) {{ Start-Sleep -Milliseconds 30 }}; "
        'Write-Output "ACTUAL_NATIVE_START_STDOUT"')
    if sys.platform != "win32":
        code = ("import time\nfrom pathlib import Path\n"
            f"Path({str(entered)!r}).write_text('ACTUAL_NATIVE_EXECUTION', encoding='utf-8')\n"
            f"while not Path({str(release)!r}).exists():\n    time.sleep(0.03)\n"
            "print('ACTUAL_NATIVE_START_STDOUT')\n")
        command = shlex.join([sys.executable, "-c", code])
    arguments = {"command": command, "login": False, "yield_time_ms": 10000}
    if shape != "direct":
        host.registry.register(ToolExecTool())
        host.registry.register(ToolWaitTool())
    if shape == "code_mode":
        import json
        call = ToolCallEvent(id="parent-script", name="tool_exec", arguments={
            "code": "text(await tools.run_command(" + json.dumps(arguments) + "));", "yield_time_ms": 10000})
    else:
        call = ToolCallEvent(id="actual-direct-native-command", name="run_command", arguments=arguments)
    settings = AgentSettings(max_iterations=3, max_turn_seconds=30, stream_max_attempts=0, code_mode_only=False)
    budget = TokenBudget(total=96000)
    config = replace(host.snapshot.config, agent=settings, token_budget=budget)
    model = NativeCommandModel(config.llm, call)
    admitted = replace(host.snapshot, config=config, llm=model)
    runtime = AgentRuntime(metrics_file=tmp_path / "runtime/metrics.jsonl", swarm_store_dir=tmp_path / "runtime/swarm", enable_lease_heartbeat=False)
    journal = ExecutionJournal("actual-query-native-command", base_dir=tmp_path / "journal")
    run = RunContext(agent_runtime=runtime, execution_journal=journal, model_execution=admitted,
        exec_policy_owner=host.owner)
    builder = ContextBuilder(llm=model, token_budget=budget, conversation_id="actual-native-owner", workspace_root=host.workspace)
    session = AgentSession(llm=model, tool_registry=host.registry, artifact_store=host.context.artifact_store,
        permission_checker=PermissionChecker(PermissionSettings(require_confirm=[], require_diff_review=[]), host.workspace),
        agent_settings=settings, token_budget=budget, context_builder=builder)
    callbacks = []

    async def emit(kind, data):
        if kind == "runtime.span" and data.get("event") == "tool.started" and data.get("tool_name") == "run_command":
            # This is the real host callback, not a manual journal consumer.
            cold = ExecutionJournal(journal.agent_id, base_dir=tmp_path / "journal")
            actual = [event.payload["tool_call"] for event in cold.read_events()
                if event.event_type == "tool_use" and event.payload["tool_call"].get("execution_started_at") == data["started_at"]]
            assert len(actual) == 1 and actual[0]["id"] == data["tool_call_id"]
            assert not any(event.event_type == "tool_result" for event in cold.read_events())
            callbacks.append(dict(data))

    state = AgentState(user_message="Run the controlled native command and wait for its real completion.",
        conversation_id="actual-native-owner", workspace_root=host.workspace)
    events = []
    async def execute():
        async for event in QueryEngine().submit(QuerySubmission(user_message=state.user_message, session=session, state=state,
            runtime=AgentLoopSessionContext(session_id="actual-native-session", workspace_root=host.workspace, run_context=run,
                permission_context=PermissionContext(mode="bypass", approval_policy="never", sandbox_mode="danger-full-access", workspace_root=host.workspace),
                cancel_event=asyncio.Event(), emit_event=emit if with_host_callback else None))):
            events.append(event)

    task = asyncio.create_task(execute())
    try:
        async with asyncio.timeout(12):
            while not entered.exists() and not task.done():
                await asyncio.sleep(.02)
        assert entered.exists(), [(event.type, event.data) for event in events if event.type in {"error", "done", "tool_result"}]
        assert entered.read_text().strip() == "ACTUAL_NATIVE_EXECUTION"
        assert not task.done()
        cold = ExecutionJournal(journal.agent_id, base_dir=tmp_path / "journal")
        claims = [event.payload["tool_call"] for event in cold.read_events()
            if event.event_type == "tool_use" and event.payload["tool_call"]["name"] == "run_command"]
        assert len(claims) == 2
        assert "execution_started_at" not in claims[0]
        assert claims[1]["execution_started_at"] >= claims[0]["command_presentation"]["started_at_ms"]
        assert claims[0]["id"] == claims[1]["id"] and claims[0]["command_presentation"] == claims[1]["command_presentation"]
        assert not any(event.event_type == "tool_result" for event in cold.read_events())
        [pending] = [item for item in cold.unresolved_tool_uses() if item["tool_name"] == "run_command"]
        assert pending["execution_started_at"] == claims[1]["execution_started_at"]
        assert pending["command_actions"] == claims[1]["command_actions"]
        assert len(callbacks) == (1 if with_host_callback and shape != "code_mode" else 0)
        release.write_text("Release the actual process", encoding="utf-8")
        await task
        assert state.terminal_status == "completed", [(event.type, event.data) for event in events if event.type in {"error", "done"}]
        result = next(event.payload for event in cold.read_events() if event.event_type == "tool_result" and event.payload["tool_name"] == "run_command")
        assert result["execution_started_at"] == pending["execution_started_at"] and result["exit_code"] == 0
        assert "ACTUAL_NATIVE_START_STDOUT" in result["stdout_preview"]
        assert result["command_actions"] == pending["command_actions"]
        assert cold.unresolved_tool_uses() == []
    finally:
        release.write_text("Release owned process during cleanup", encoding="utf-8")
        try:
            await task
        finally:
            await session.aclose()
            session.artifact_store.shutdown()
            runtime.close(release_lease=True)


@pytest.mark.asyncio
async def test_actual_old_callback_cannot_replace_new_turn_clock_when_provider_reuses_call_id(tmp_path):
    host = setup(tmp_path, approval_policy="never")
    arguments = {"command": output_command("REAL_REUSED_NATIVE_CALL")[0], "login": False, "yield_time_ms": 10000}
    model = NativeCommandModel(host.snapshot.config.llm, ToolCallEvent(id="reused-native-id", name="run_command", arguments=arguments))
    settings, budget = AgentSettings(max_iterations=3, max_turn_seconds=30, stream_max_attempts=0, code_mode_only=False), TokenBudget(total=96000)
    admitted = replace(host.snapshot, config=replace(host.snapshot.config, agent=settings, token_budget=budget), llm=model)
    runtime = AgentRuntime(metrics_file=tmp_path / "runtime/metrics.jsonl", swarm_store_dir=tmp_path / "runtime/swarm", enable_lease_heartbeat=False)
    journal = ExecutionJournal("actual-reused-native-call", base_dir=tmp_path / "journal")
    builder = ContextBuilder(llm=model, token_budget=budget, conversation_id="actual-reused-owner", workspace_root=host.workspace)
    session = AgentSession(llm=model, tool_registry=host.registry, artifact_store=host.context.artifact_store,
        permission_checker=host.checker, agent_settings=settings, token_budget=budget, context_builder=builder)
    original = []
    new_start = []

    async def emit(kind, data):
        if kind != "runtime.span" or data.get("event") != "tool.started" or data.get("tool_name") != "run_command":
            return
        if not original:
            original.append((dict(data), builder._tool_execution_context.emit_event))
            return
        if data["turn_id"] == original[0][0]["turn_id"]:
            # A real WS host also rejects projection into its older turn.
            return
        assert (data["turn_id"], data["iteration_id"]) != (original[0][0]["turn_id"], original[0][0]["iteration_id"])
        cold = ExecutionJournal(journal.agent_id, base_dir=tmp_path / "journal")
        before = cold.unresolved_tool_uses()[0]
        assert before["execution_started_at"] == data["started_at"]
        # Deliver the actual old ToolExecutionContext callback and its original
        # event after the same id's next physical request is already open.
        await original[0][1](kind, dict(original[0][0]))
        after = cold.unresolved_tool_uses()[0]
        assert after["execution_started_at"] == data["started_at"]
        claims = [event.payload["tool_call"] for event in cold.read_events() if event.event_type == "tool_use"
            and event.payload["tool_call"].get("turn_id") == data["turn_id"]]
        assert len(claims) == 2 and claims[-1]["iteration_id"] == data["iteration_id"]
        new_start.append(dict(data))

    try:
        for _ in range(2):
            model.calls = 0
            model.call = ToolCallEvent(id="reused-native-id", name="run_command", arguments=dict(arguments))
            run = RunContext(agent_runtime=runtime, execution_journal=journal, model_execution=admitted, exec_policy_owner=host.owner)
            state = AgentState(user_message="Execute one actual native command in this physical turn.",
                conversation_id="actual-reused-owner", workspace_root=host.workspace)
            events = [event async for event in QueryEngine().submit(QuerySubmission(user_message=state.user_message, session=session, state=state,
                runtime=AgentLoopSessionContext(session_id="actual-reused-session", workspace_root=host.workspace, run_context=run,
                    permission_context=PermissionContext(mode="bypass", approval_policy="never", sandbox_mode="danger-full-access", workspace_root=host.workspace),
                    cancel_event=asyncio.Event(), emit_event=emit)))]
            assert state.terminal_status == "completed", [(event.type, event.data) for event in events if event.type in {"error", "done"}]
        assert len(original) == len(new_start) == 1
        assert original[0][0]["turn_id"] != new_start[0]["turn_id"]
        results = [event.payload for event in journal.read_events() if event.event_type == "tool_result"]
        assert len(results) == 2 and all(result["exit_code"] == 0 for result in results)
        assert results[-1]["execution_started_at"] == new_start[0]["started_at"]
        assert journal.unresolved_tool_uses() == []
    finally:
        await session.aclose()
        session.artifact_store.shutdown()
        runtime.close(release_lease=True)
