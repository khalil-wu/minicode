from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import get_args
from unittest.mock import AsyncMock

import pytest

from backend.agent.conversation_query_guard import conversation_query_guards
from backend.agent.runtime import AgentRuntime
from backend.conversations.repository import ConversationRepository
from backend.memory import consolidation_agent
from backend.services.checkpoint_service import RunCheckpointResumeResult
from backend.terminal.task_persistence import PersistedTaskState
from backend.tools.agent_control_plane import AgentControlPlane
from backend.artifact.store import ArtifactStore
from backend.services.tool_registry_factory import build_tool_registry
from backend.ws.events import ClientCommandType
from backend.ws.handler import WebSocketSession
from backend.ws.handlers.conversation import handle_context_compact
from backend.ws.handlers.misc import HANDLERS, handle_agent_resume
from backend.ws.session_lifecycle import SessionLifecycle


def test_agent_paths_have_one_minicode_root(tmp_path) -> None:
    runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl")
    try:
        control = AgentControlPlane(None, runtime=runtime)
        assert control.normalize_public_tree_path("/root/worker", strict=True) == "/root/worker"
        with pytest.raises(ValueError, match="must start with `/root`"):
            control.normalize_public_tree_path("/morpheus", strict=True)
    finally:
        runtime.close(release_lease=True)


def test_legacy_control_commands_are_not_executable() -> None:
    commands = set(get_args(ClientCommandType))

    assert "control_response" in commands
    assert "control_cancel_request" in commands
    assert {"approval", "answer", "task.edit"}.isdisjoint(commands)
    assert "task.edit" not in HANDLERS


def test_update_plan_is_the_only_builtin_checklist_writer(tmp_path) -> None:
    registry = build_tool_registry(
        ArtifactStore(storage_dir=tmp_path / "artifacts"),
    )

    assert registry.get_tool("update_plan") is not None
    assert registry.get_tool("todo_write") is None
    assert registry.get_tool("todo_read") is None


def test_ws_resume_preserves_checkpoint_as_provenance_not_new_run_identity(monkeypatch) -> None:
    resume = RunCheckpointResumeResult(
        session_id="session-1",
        conversation_id="conversation-1",
        run_id="old-checkpoint-run",
        iteration=7,
        stopped_reason="interrupted",
        user_message="continue",
    )
    monkeypatch.setattr(
        "backend.services.checkpoint_service.prepare_run_checkpoint_resume",
        lambda **_kwargs: resume,
    )
    session = SimpleNamespace(
        session_id="session-1",
        active_conversation_id="conversation-1",
        conversation_repo=SimpleNamespace(
            get_conversation=lambda _conversation_id: SimpleNamespace(
                workspace_root="",
                worktree_path="",
            )
        ),
        send_payload=AsyncMock(return_value=True),
        start_agent_run=AsyncMock(return_value="new-task"),
        emit_command_result=AsyncMock(),
        ws_manager=None,
    )
    session.session_lifecycle = SessionLifecycle(session)
    session.resolve_requested_workspace = lambda requested=None: Path(
        requested or "."
    ).expanduser().resolve()

    assert asyncio.run(handle_agent_resume(session, {})) is True

    payload = session.send_payload.await_args.args[0]
    assert payload["checkpoint_run_id"] == "old-checkpoint-run"
    assert "run_id" not in payload
    metadata = session.start_agent_run.await_args.kwargs["metadata"]
    assert metadata["resume_checkpoint_run_id"] == "old-checkpoint-run"
    assert "run_id" not in metadata


def test_manual_compaction_rejects_an_active_conversation_turn() -> None:
    guard = conversation_query_guards()
    claim = guard.try_start("conversation-compact-busy", owner_id="active-turn")
    assert claim is not None
    builder = SimpleNamespace(
        compact=AsyncMock(),
        export_snapshot=lambda: {},
    )
    session = SimpleNamespace(
        active_conversation_id="conversation-compact-busy",
        ws_manager=None,
        context_builder=builder,
        conversation_repo=SimpleNamespace(
            get_conversation=lambda _conversation_id: SimpleNamespace(
                revision=1,
                context_snapshot={},
            )
        ),
        send_event=AsyncMock(),
    )
    try:
        assert asyncio.run(handle_context_compact(session, {})) is True
    finally:
        assert guard.end(claim)

    builder.compact.assert_not_awaited()
    event = session.send_event.await_args.args[0]
    assert event.type == "error"
    assert event.data["error_type"] == "conversation_busy"


@pytest.mark.parametrize(
    ("fail_on_call", "expected_error_code", "expected_commits"),
    [
        (1, "context.compact_failed", 0),
        (2, "context.budget_refresh_failed", 1),
    ],
)
def test_manual_compaction_reports_budget_failure_at_the_correct_commit_boundary(
    monkeypatch,
    fail_on_call: int,
    expected_error_code: str,
    expected_commits: int,
) -> None:
    from backend.services import context_budget

    calls = 0

    def budget_snapshot(_session, _builder):
        nonlocal calls
        calls += 1
        if calls == fail_on_call:
            raise RuntimeError("token estimate unavailable")
        return {"used": 100, "total": 1000, "breakdown": {}}

    compact = AsyncMock(return_value=SimpleNamespace(summary="Saved summary"))
    monkeypatch.setattr(context_budget, "build_context_budget_snapshot", budget_snapshot)
    monkeypatch.setattr("backend.ws.compaction_coordinator.compact_conversation", compact)
    monkeypatch.setattr("backend.ws.handlers.conversation._conversation_has_active_run", lambda *_args: False)
    session = SimpleNamespace(
        active_conversation_id="conversation-budget-failure",
        context_builder=SimpleNamespace(context_ledger=lambda: {"estimated_tokens": 100, "entries": []}),
        conversation_repo=SimpleNamespace(get_conversation=lambda _id: object()),
        send_event=AsyncMock(),
        last_agent_state=None,
    )

    assert asyncio.run(handle_context_compact(session, {})) is True
    assert compact.await_count == expected_commits
    events = [call.args[0] for call in session.send_event.await_args_list]
    assert [event.type for event in events] == (
        ["conversation.compaction.updated", "error"] if expected_commits else ["error"]
    )
    assert events[-1].data["error_code"] == expected_error_code
    if expected_commits:
        assert events[0].data["summary"] == "Saved summary"
        assert "compacted and saved" in events[-1].data["message"]


def test_manual_compaction_keeps_committed_result_when_event_delivery_fails(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("backend.ws.handlers.conversation._conversation_has_active_run", lambda *_args: False)
    repo = ConversationRepository(tmp_path / "conversations")
    conversation = repo.create_conversation(conversation_id="conversation-committed-before-send")

    class Builder:
        compacted = False

        def context_ledger(self):
            return {"estimated_tokens": 100, "actual_tokens": 100, "entries": []}

        def get_budget_snapshot(self, *, state, tool_schemas):
            return {"used": 100, "total": 1000, "breakdown": {"history": 100}}

        async def compact(self, *, focus, restore_state):
            self.compacted = True
            return "Saved summary"

        def export_snapshot(self):
            return {"history": [], "compaction_count": 1}

    send_event = AsyncMock(side_effect=OSError("socket send failed"))
    session = SimpleNamespace(
        active_conversation_id=conversation.id,
        context_builder=Builder(),
        last_agent_state=None,
        tool_registry=SimpleNamespace(get_schemas=lambda **_kwargs: []),
        permission_checker=object(),
        permission_context=object(),
        conversation_repo=repo,
        conversation_runtime=None,
        _conversation_projection_lock=lambda _id: asyncio.Lock(),
        send_event=send_event,
    )

    with pytest.raises(OSError, match="socket send failed"):
        asyncio.run(handle_context_compact(session, {}))

    saved = repo.get_conversation(conversation.id)
    assert saved is not None
    assert saved.compaction_state == "compacted"
    assert saved.compaction_summary == "Saved summary"
    assert saved.context_snapshot["compaction_count"] == 1
    assert send_event.await_count == 1
    assert send_event.await_args.args[0].type == "conversation.compaction.updated"


def test_memory_consolidation_uses_query_engine_lifecycle(monkeypatch, tmp_path) -> None:
    submissions = []

    class _Engine:
        async def submit(self, submission):
            submissions.append(submission)
            submission.state.terminal_status = "completed"
            if False:
                yield None

    monkeypatch.setattr(consolidation_agent, "QueryEngine", _Engine)

    asyncio.run(consolidation_agent.run_memory_consolidation_agent(
        llm=object(),
        memory_root=tmp_path,
        prompt="consolidate",
    ))

    assert len(submissions) == 1
    submission = submissions[0]
    assert submission.runtime.metadata["agent_role"] == "memory_consolidation"
    assert submission.runtime.workspace_root == tmp_path.resolve()
    assert submission.session.context_builder is not None


def test_terminal_subagent_cleanup_fails_closed_without_resource_owner(monkeypatch, tmp_path) -> None:
    runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl")
    parent = runtime.start_run(run_id="parent", conversation_id="conversation")
    child = runtime.start_subagent(
        subagent_id="child",
        parent_run_id=parent.run_id,
        agent_type="general-purpose",
        background=True,
    )
    runtime._mark_subagent_cleanup(child.subagent_id, reason="cancelled")
    record = runtime.get_subagent(child.subagent_id)
    assert record is not None
    record.cleanup_resources.append({
        "resource_kind": "background_command",
        "resource_id": "command-1",
        "state": "active",
    })
    monkeypatch.setattr(runtime, "_reconcile_subagent_worktrees", lambda _record: True)

    assert runtime._reconcile_terminal_subagent_cleanup(child.subagent_id) is False
    assert runtime.get_subagent(child.subagent_id).cleanup_pending is True
    runtime.close()


def test_terminal_subagent_cleanup_uses_explicit_terminal_authority(monkeypatch, tmp_path) -> None:
    runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl")
    parent = runtime.start_run(
        run_id="parent-owned",
        conversation_id="conversation",
        session_id="session-owned",
    )
    child = runtime.start_subagent(
        subagent_id="child-owned",
        parent_run_id=parent.run_id,
        agent_type="general-purpose",
        background=True,
        session_id="session-owned",
    )
    runtime._mark_subagent_cleanup(child.subagent_id, reason="cancelled")
    observed = {}

    def reconcile(_session_id, **kwargs):
        observed.update(kwargs)
        return SimpleNamespace(completed=True, pending_task_ids=(), errors=())

    monkeypatch.setattr(runtime, "_reconcile_subagent_worktrees", lambda _record: True)
    monkeypatch.setattr("backend.terminal.task_persistence.reconcile_owned_tasks", reconcile)

    assert runtime._reconcile_terminal_subagent_cleanup(child.subagent_id) is True
    assert observed["owner_terminal"] is True
    assert runtime.get_subagent(child.subagent_id).cleanup_pending is False
    runtime.close()


def test_websocket_projects_recovered_background_terminal_once() -> None:
    recovered = PersistedTaskState(
        task_id="bg-recovered",
        command="npm run dev",
        description="dev server",
        cwd="C:/repo",
        pid=None,
        started_at=10.0,
        timeout_ms=0,
        status="interrupted",
        conversation_id="conversation-1",
        owner_task_id="turn-1",
        parent_run_id="run-1",
        cleanup_pending=False,
        cleanup_reason="background_owner_exited",
        cleanup_requested_at=11.0,
        cleanup_completed_at=12.0,
    )
    manager = SimpleNamespace(
        cleanup_orphaned_tasks_on_startup=lambda: [recovered]
    )
    session = SimpleNamespace(
        session_id="session-1",
        background_manager=manager,
        send_event=AsyncMock(return_value=True),
        send_payload=AsyncMock(return_value=True),
        is_connected=True,
    )
    lifecycle = SessionLifecycle(session)

    async def scenario() -> None:
        await lifecycle.recover_orphaned_background_commands()
        await lifecycle.recover_orphaned_background_commands()

    asyncio.run(scenario())

    session.send_payload.assert_awaited_once()
    payload = session.send_payload.await_args.args[0]
    assert payload["type"] == "background.completed"
    assert payload["status"] == "interrupted"
    assert payload["conversation_id"] == "conversation-1"
    assert payload["task_id"] == "turn-1"
    assert payload["parent_run_id"] == "run-1"
    assert payload["cleanup_pending"] is False


def test_websocket_does_not_guess_owner_for_recovered_background_terminal() -> None:
    recovered = PersistedTaskState(
        task_id="bg-ownerless",
        command="python worker.py",
        description="worker",
        cwd="C:/repo",
        pid=None,
        started_at=10.0,
        timeout_ms=0,
        status="interrupted",
        cleanup_pending=False,
        cleanup_reason="background_owner_exited",
    )
    session = SimpleNamespace(
        session_id="session-1",
        background_manager=SimpleNamespace(
            cleanup_orphaned_tasks_on_startup=lambda: [recovered]
        ),
        send_event=AsyncMock(return_value=True),
        send_payload=AsyncMock(return_value=True),
        is_connected=True,
    )

    asyncio.run(SessionLifecycle(session).recover_orphaned_background_commands())

    session.send_payload.assert_not_awaited()
    event = session.send_event.await_args.args[0]
    assert event.type == "error"
    assert event.data["error_code"] == "background.owner_missing"
