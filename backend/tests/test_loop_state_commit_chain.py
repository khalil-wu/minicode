from __future__ import annotations

import asyncio
import subprocess
from contextlib import aclosing
from dataclasses import asdict

import pytest

from backend.agent.context import ContextBuilder
from backend.agent.execution_journal import ExecutionJournal
from backend.agent.execution_lifecycle import ExecutionLifecycle
from backend.agent.loop_session import AgentLoopSessionContext
from backend.agent.query_engine import AgentSession, QueryEngine, QuerySubmission
from backend.agent.query_journal import QueryJournalRecorder
from backend.agent.run_context import RunContext
from backend.agent.state import AgentState, ToolCallRecord
from backend.agent.message import AgentEvent
from backend.agent.tool_batch_execution import execute_tool_batch
from backend.agent.turn_state import AgentTurnState
from backend.agent import worktree
from backend.artifact.store import ArtifactStore
from backend.config import AgentSettings, PermissionSettings, TokenBudget
from backend.llm.base import LLMAdapter, StreamEvent, StreamEventType, ToolCallEvent
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema
from backend.tools.registry import ToolRegistry


class OutputTool(BaseTool):
    name = "buffered_observation"
    description = "Record-only completed output"
    permission = PermissionLevel.AUTO
    read_only = True
    streams_output = True
    def get_schema(self):
        return ToolSchema(self.name, self.description, {"type": "object", "properties": {}})
    async def execute(self, args, context=None):
        return ToolResult("ACTUAL_OBSERVATION")


@pytest.mark.parametrize("parallel", [False, True])
def test_buffered_delta_close_preserves_every_already_completed_result(tmp_path, parallel):
    async def run():
        registry = ToolRegistry()
        registry.register(OutputTool())
        state = AgentState(user_message="record", iterations=1)
        calls = [ToolCallEvent(id=f"call-{i}", name="buffered_observation", arguments={}) for i in range(2 if parallel else 1)]
        builder = ContextBuilder(conversation_id="batch", workspace_root=tmp_path)
        builder.append_assistant_tool_calls(calls)
        checker = PermissionChecker(PermissionSettings(), tmp_path)
        context = ToolExecutionContext(permission=PermissionContext(mode="bypass"), workspace_root=tmp_path, permission_checker=checker,
            conversation_id="batch", metadata={"run_id": "exact-run"})
        async with aclosing(execute_tool_batch(calls, ctx=builder, state=state, tool_registry=registry, permission_checker=checker,
            approval_handler=None, skill_manager=None, permission_context=context.permission, tool_ctx=context)) as events:
            async for event in events:
                if event.type == "tool_output_delta":
                    break
        await asyncio.sleep(0)
        assert len(state.tool_calls) == len(calls)
        assert [record.tool_call_id for record in state.tool_calls] == [call.id for call in calls]
        assert len([message for message in builder._history if message.role == "tool"]) == len(calls)
        assert all(record.status == "success" and record.result_payload["id"] == record.tool_call_id for record in state.tool_calls)
    asyncio.run(run())


class ToolModel(LLMAdapter):
    async def stream_chat(self, messages, tools=None, metadata=None):
        yield StreamEvent(type=StreamEventType.TOOL_CALL, tool_calls=[ToolCallEvent(id="exact-call", name="buffered_observation", arguments={})])
        yield StreamEvent(type=StreamEventType.DONE, finish_reason="tool_calls")
    async def simple_chat(self, messages, **kwargs):
        return ""


def test_close_after_first_delta_reconciles_real_journal_recovery_and_ui(tmp_path):
    async def run():
        registry = ToolRegistry()
        registry.register(OutputTool())
        journal = ExecutionJournal("loop-commit", base_dir=tmp_path / "journal")
        state = AgentState(user_message="record")
        context = ContextBuilder(conversation_id="durable", workspace_root=tmp_path)
        session = AgentSession(llm=ToolModel(), tool_registry=registry, artifact_store=ArtifactStore(storage_dir=tmp_path / "artifacts"),
            permission_checker=PermissionChecker(PermissionSettings(), tmp_path), agent_settings=AgentSettings(max_iterations=1), token_budget=TokenBudget(), context_builder=context)
        ui = AgentTurnState(now_ms=lambda: 9000)
        try:
            async with aclosing(QueryEngine().submit(QuerySubmission(user_message="record", state=state, session=session,
                runtime=AgentLoopSessionContext(workspace_root=tmp_path, permission_context=PermissionContext(mode="bypass"),
                    run_context=RunContext(execution_journal=journal))))) as events:
                async for event in events:
                    if event.type == "tool_call":
                        ui.record_tool_call(event.data)
                    elif event.type == "tool_output_delta":
                        ui.record_tool_output_delta(event.data)
                        break
            assert state.tool_calls and state.tool_calls[-1].tool_call_id == "exact-call"
            assert not journal.unresolved_tool_uses()
            results = [event.payload for event in journal.read_events() if event.event_type == "tool_result"]
            assert len(results) == 1 and results[0]["status"] == "success"
            restored = [ToolCallRecord(**asdict(record)) for record in state.tool_calls]
            ui.reconcile_committed_tool_results(restored)
            snapshot = ui.finalize(terminal_status="cancelled")
            assert snapshot.tool_calls[0]["status"] == "success"
            assert "ACTUAL_OBSERVATION" in snapshot.tool_calls[0]["summary"]
            assert snapshot.tool_calls[0]["finishedAt"] == state.tool_calls[0].result_payload["completed_at_ms"]
            recovered = journal.reconstruct_history()
            assert "ACTUAL_OBSERVATION" in str(recovered)
        finally:
            await session.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("change", ["id", "turn", "iteration", "legacy"])
def test_ui_committed_reconciliation_never_guesses_another_execution(change):
    ui = AgentTurnState(now_ms=lambda: 500)
    ui.record_tool_call({"id": "call", "name": "buffered_observation", "turn_id": "run", "iteration_id": "iter:1"})
    record = ToolCallRecord(tool_name="buffered_observation", tool_call_id="call", turn_id="run", iteration_id="iter:1",
        result_payload={"id": "call", "status": "success", "summary": "observed"})
    if change == "id":
        record.tool_call_id = "foreign"
    elif change == "turn":
        record.turn_id = "foreign"
    elif change == "iteration":
        record.iteration_id = "iter:2"
    else:
        record.tool_call_id = ""
    ui.reconcile_committed_tool_results([record])
    assert ui.finalize(terminal_status="cancelled").tool_calls[0]["status"] == "cancelled"


def test_cleanup_phase_does_not_rewrite_actual_finish_timestamp(monkeypatch):
    values = iter((100, 200))
    monkeypatch.setattr("backend.agent.execution_lifecycle.epoch_ms", lambda: next(values))
    lifecycle = ExecutionLifecycle(run_id="run", started_at=10, updated_at=10)
    lifecycle.transition(phase="failed", status="failed")
    lifecycle.transition(phase="cleanup_pending", status="failed")
    assert lifecycle.completed_at == 100 and lifecycle.updated_at == 200


def test_journal_terminal_never_uses_an_earlier_iteration_receipt(tmp_path):
    journal = ExecutionJournal("exact-claim", base_dir=tmp_path / "journal")
    journal.append_tool_use({"id": "reused", "name": "buffered_observation", "args": {},
        "turn_id": "run", "iteration_id": "iter:2", "request_digest": "digest"})
    state = AgentState(user_message="record", tool_calls=[ToolCallRecord(tool_name="buffered_observation", tool_call_id="reused",
        turn_id="run", iteration_id="iter:1", request_digest="digest",
        result_payload={"id": "reused", "status": "success", "summary": "EARLIER_OBSERVATION"})])
    recorder = QueryJournalRecorder(journal=journal, metadata={"run_id": "run"}, state=state,
        context_builder=ContextBuilder(), turn_kernel=None, conversation_id="exact")
    recorder.runtime_terminal_receipt_recorded = True
    recorder.record_terminal(AgentEvent.done(status="cancelled", reason="interrupted"))
    results = [event.payload for event in journal.read_events() if event.event_type == "tool_result"]
    assert len(results) == 1 and "EARLIER_OBSERVATION" not in str(results)


def test_ignored_agent_outputs_are_retained_without_a_remove_command(tmp_path, monkeypatch):
    calls = []
    info = worktree.AgentWorktree(tmp_path / "child", "codex/audit", "head", tmp_path)
    def git(cwd, *args):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "!! output/report.pdf\n" if "--ignored" in args else "", "")
    monkeypatch.setattr(worktree, "_git", git)
    monkeypatch.setattr(worktree, "remove_agent_worktree", lambda info: pytest.fail("ignored deliverable was removed"))
    kept, path = worktree.cleanup_agent_worktree(info)
    assert kept and path == str(info.worktree_path)
    assert calls == [("status", "--porcelain", "--ignored")]


def test_failed_stale_prune_is_not_recorded_as_a_finished_sweep(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(worktree, "find_git_root", lambda base: tmp_path)
    monkeypatch.setattr(worktree, "_STALE_SWEEP_DONE", set())
    def git(cwd, *args):
        calls.append(args)
        return subprocess.CompletedProcess(args, 1 if len(calls) == 1 else 0, "", "")
    monkeypatch.setattr(worktree, "_git", git)
    for _ in range(3):
        worktree.cleanup_stale_worktrees(tmp_path)
    assert len(calls) == 2
