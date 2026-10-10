from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest

from backend.agent.context import ContextBuilder
from backend.agent.execution_journal import ExecutionJournal, execution_journal_owner
from backend.agent.loop import AgentLoopSessionContext
from backend.agent.message import AgentEvent, UserCommand
from backend.agent.query_engine import AgentSession, QueryEngine, QuerySubmission
from backend.agent.query_journal import QueryJournalRecorder
from backend.agent.run_context import RunContext
from backend.agent.runtime import AgentRuntime
from backend.agent.swarm_store import FileSwarmStore
from backend.artifact.store import ArtifactStore
from backend.config import AgentSettings, PermissionSettings, TokenBudget
from backend.conversations.context_delta import context_snapshot_delta
from backend.conversations.repository import ConversationRepository
from backend.permissions.checker import PermissionChecker
from backend.tools.registry import ToolRegistry
from backend.ws.command_dispatcher import SessionCommandDispatcher
from backend.ws.durable_user_queue import DurableUserMessageQueue
from backend.ws.event_outbox import EventOutbox
from backend.ws.handlers.conversation import handle_conversation_create
from backend.ws.run_manager import SessionRunManager
from backend.ws.session_restore import SessionRestoreManager


async def _terminal_child(root: Path, boundary: str) -> None:
    runtime = AgentRuntime(metrics_file=root / "metrics.jsonl", swarm_store_dir=root / "swarm",
                           enable_lease_heartbeat=False)
    repository = ConversationRepository(root / "conversations")
    conversation = repository.create_conversation(conversation_id="conv-final-crash")
    owner = execution_journal_owner("conversation", repository.store_instance_id(), conversation.id)
    journal = runtime.execution_journal(owner)
    context = ContextBuilder()
    record_event = QueryJournalRecorder.record_event

    def crash_at_receipt(recorder, event):
        if event.type == "agent.run.completed" and boundary == "before_receipt":
            os._exit(74)
        record_event(recorder, event)
        if event.type == "agent.run.completed" and boundary == "after_partial_receipt":
            os._exit(75)

    QueryJournalRecorder.record_event = crash_at_receipt

    async def runner(**kwargs):
        state = kwargs["state"]
        context.append_user("Finish the task")
        context.record_turn_admission("user-final", {
            "history_start": 0, "history_end": 1, "run_id": "run-final", "client_command_id": "cmd-final",
        })
        admitted = repository.commit_turn_admission(conversation.id,
            user_message={"id": "user-final", "role": "user", "content": "Finish the task"},
            context_snapshot=context.export_snapshot())
        if boundary == "after_partial_receipt":
            partial = {"id": "assistant-final", "role": "assistant", "content": "partial prefix",
                       "terminal_status": "partial", "termination_reason": "run_in_progress"}
            delta = context_snapshot_delta(admitted.context_snapshot, context.export_snapshot())
            pending = journal.append_lifecycle("conversation_projection_pending", {
                "conversation_id": conversation.id, "run_id": "run-final", "assistant_message": partial,
                "context_delta": delta, "partial": True, "expected_revision": admitted.revision,
                "source_user_message_ids": ["user-final"],
            })
            committed = repository.commit_turn_projection(conversation.id, assistant_message=partial,
                context_delta=delta, partial=True, expected_revision=admitted.revision)
            journal.append_lifecycle("conversation_projection_committed", {
                "conversation_id": conversation.id, "run_id": "run-final", "message_id": "assistant-final",
                "pending_event_id": pending.event_id, "partial": True, "conversation_revision": committed.revision,
            })
        context.append_assistant("complete final answer")
        state.reply = "complete final answer"
        state.stopped_reason = "completed"
        yield AgentEvent.done(status="completed")

    submission = QuerySubmission(user_message="Finish the task", session=AgentSession(
        llm=object(), tool_registry=ToolRegistry(), artifact_store=ArtifactStore(storage_dir=root / "artifacts"),
        permission_checker=PermissionChecker(PermissionSettings()), agent_settings=AgentSettings(),
        token_budget=TokenBudget(), context_builder=context,
    ), runtime=AgentLoopSessionContext(session_id="final-session", task_id="final-task", metadata={
        "run_id": "run-final", "conversation_id": conversation.id, "assistant_message_id": "assistant-final",
        "user_message_id": "user-final", "conversation_store_id": repository.store_instance_id(),
    }, run_context=RunContext(agent_runtime=runtime, execution_journal=journal)))
    async for _ in QueryEngine(runner=runner).submit(submission):
        pass
    raise AssertionError("terminal crash boundary was not reached")


async def _create_session(root: Path):
    queue = DurableUserMessageQueue(session_id="create-session", root_dir=root / "queue")
    session = SimpleNamespace(session_id="create-session", ws_manager=None, is_connected=False,
        active_conversation_id=None, _extension_shutdown_requested=False, _model_override_active=False,
        config=SimpleNamespace(llm=SimpleNamespace(reasoning_effort="")), provider="fixture", selected_model="fixture",
        artifact_store=None, conversation_repo=ConversationRepository(root / "conversations"),
        run_manager=SimpleNamespace(durable_client_commands=queue), connection_generation=1)
    lock = asyncio.Lock()
    session.conversation_lifecycle_lock = lambda: lock
    session.load_active_conversation_snapshot = lambda *args, **kwargs: False
    session.sync_permission_mode_with_active_conversation = lambda **kwargs: None
    session.runtime_snapshot = lambda **kwargs: {"session_id": session.session_id}
    session.session_lifecycle = SimpleNamespace(clear_workspace_runtime=lambda: None,
                                               workspace_root_for_conversation=lambda *args: None)
    async def send_payload(payload, **kwargs):
        return True
    async def send_event(event):
        pass
    async def emit_command_result(*args, **kwargs):
        pass
    async def dispatch(kind, data):
        return await handle_conversation_create(session, data)
    session.send_payload, session.send_event, session.emit_command_result = send_payload, send_event, emit_command_result
    session.command_registry = SimpleNamespace(dispatch=dispatch)
    session.event_outbox = EventOutbox(session_id=session.session_id, websocket=None,
        replay_root=root / "replay", replay_limit=30, cleanup_tasks=set(), has_active_run=lambda: False,
        requires_conversation_owner=lambda *args: False, workspace_scoped_event_types=())
    session.command_dispatcher = SessionCommandDispatcher(session, root_dir=root / "dedup")
    return session, queue


async def _create_child(root: Path, identity: str) -> None:
    session, queue = await _create_session(root)
    if identity == "occupied":
        session.conversation_repo.create_conversation(conversation_id="conv-requested", title="another task")
    command = UserCommand("conversation.create", {
        "client_command_id": "cmd-create", "title": "created task", "conversation_type": "main",
        **({"conversation_id": "conv-requested"} if identity != "generated" else {}),
    })
    queue.persist_client_command(command)
    def crash_before_dedup(*args, **kwargs):
        (root / "created-id.txt").write_text(session.active_conversation_id, encoding="utf-8")
        os._exit(73)
    session.command_dispatcher._mark_client_command_seen = crash_before_dedup
    await session.command_dispatcher._run_durable_client_command("cmd-create", 1)
    raise AssertionError("creation crash boundary was not reached")


def _kill_probe(tmp_path: Path, kind: str, boundary: str, expected_code: int):
    process = subprocess.run([sys.executable, str(Path(__file__).resolve()), kind, str(tmp_path), boundary],
        cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True)
    assert process.returncode == expected_code, process.stdout + process.stderr


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary,exit_code", [("before_receipt", 74), ("after_partial_receipt", 75)])
@pytest.mark.parametrize("owner", ["configured", "injected"])
async def test_cold_restore_recovers_exact_completed_answer_after_terminal_process_crash(tmp_path, monkeypatch, boundary, exit_code, owner):
    _kill_probe(tmp_path, "--terminal-child", boundary, exit_code)
    repository = ConversationRepository(tmp_path / "conversations")
    store = FileSwarmStore(tmp_path / "swarm")
    record = store.get_agent_run("run-final")
    assert record["status"] == "completed" and record["terminal_intent_event_id"]
    journal_owner = execution_journal_owner("conversation", repository.store_instance_id(), "conv-final-crash")
    journal = ExecutionJournal(journal_owner, base_dir=tmp_path / "sidechains", terminal_record_reader=store.get_agent_run)
    receipts = [event for event in journal.read_events() if event.payload.get("lifecycle") == "runtime_terminal_committed"]
    assert bool(receipts) is (boundary == "after_partial_receipt")
    assert journal.unprojected_terminal_projections()[0]["source_event_id"] == record["terminal_intent_event_id"]
    monkeypatch.setattr("backend.agent.runtime.SWARM_DIR", tmp_path / "swarm")
    monkeypatch.setattr("backend.agent.runtime.default_runtime_if_initialized", lambda: None)
    runtime = (AgentRuntime(metrics_file=tmp_path / "metrics.jsonl", swarm_store_dir=tmp_path / "swarm",
                            enable_lease_heartbeat=False) if owner == "injected" else None)
    manager = SessionRestoreManager(repository, agent_runtime=runtime)
    restored = await manager.restore_session("final-session", "conv-final-crash")
    assert restored["restored"] is True and restored["error"] is None
    assert [message["content"] for message in restored["messages"]] == ["Finish the task", "complete final answer"]
    assert restored["messages"][-1]["terminal_status"] == "completed"
    assert journal.unprojected_terminal_projections() == []
    again = await manager.restore_session("final-session", "conv-final-crash")
    assert again["messages"] == restored["messages"]
    if runtime is not None:
        runtime.close(release_lease=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("identity", ["requested", "generated", "occupied"])
async def test_creation_command_replay_after_process_crash_keeps_the_created_identity(tmp_path, identity):
    _kill_probe(tmp_path, "--create-child", identity, 73)
    created_id = (tmp_path / "created-id.txt").read_text(encoding="utf-8")
    session, queue = await _create_session(tmp_path)
    try:
        assert len(queue.pending_client_commands()) == 1
        await session.command_dispatcher._run_durable_client_command("cmd-create", 1)
        assert session.active_conversation_id == created_id
        conversations = session.conversation_repo.list_conversations()
        assert len(conversations) == (2 if identity == "occupied" else 1)
        created = session.conversation_repo.get_conversation(created_id)
        assert created.creation_command_id == "create-session:cmd-create"
        assert queue.pending_client_commands() == []
    finally:
        queue.close()


@pytest.mark.asyncio
async def test_deleted_creation_command_is_terminally_settled_during_replay(tmp_path):
    _kill_probe(tmp_path, "--create-child", "requested", 73)
    session, queue = await _create_session(tmp_path)
    results = []
    async def capture(event):
        results.append(event.to_ws_message())
    session.send_event = capture
    try:
        assert session.conversation_repo.delete_conversation("conv-requested")
        await session.command_dispatcher._run_durable_client_command("cmd-create", 1)
        assert queue.pending_client_commands() == []
        assert session.conversation_repo.list_conversations() == []
        assert results[-1]["type"] == "command.result" and results[-1]["level"] == "error"
        assert "deleted" in results[-1]["message"]
    finally:
        queue.close()


@pytest.mark.parametrize("requested_id", ["conv-requested", "invalid/id", None])
def test_replayed_create_cannot_resurrect_its_deleted_conversation_or_reuse_a_clone(tmp_path, requested_id):
    repository = ConversationRepository(tmp_path / "conversations")
    first = repository.create_conversation(conversation_id=requested_id, creation_command_id="session:command")
    cold = ConversationRepository(tmp_path / "conversations")
    assert cold.create_conversation(conversation_id=requested_id, creation_command_id="session:command").id == first.id
    clone = repository.clone_conversation(first.id)
    assert clone.creation_command_id == ""
    assert repository.delete_conversation(first.id)
    cold = ConversationRepository(tmp_path / "conversations")
    with pytest.raises(ValueError, match="has been deleted"):
        cold.create_conversation(conversation_id=requested_id, creation_command_id="session:command")
    assert [record.id for record in cold.list_conversations()] == [clone.id]


@pytest.mark.asyncio
async def test_delivery_fences_remain_bounded_and_keep_old_completion_until_cleanup():
    manager = SessionRunManager(SimpleNamespace(session_lifecycle=SimpleNamespace(schedule_task_runtime_update=lambda: None)))
    cancel = asyncio.Event()
    try:
        for index in range(3000):
            task = asyncio.get_running_loop().create_future()
            task.set_result(None)
            task_id = f"task-{index}"
            manager.register(conversation_id="conv-fences", task=task, task_id=task_id,
                             cancel_event=cancel, active_conversation_id=None)
            manager.mark_delivery_complete("conv-fences", task_id)
            manager.cleanup(conversation_id="conv-fences", task=task, task_id=task_id, cancel_event=cancel)
        assert manager._delivery_complete == set()
        assert manager._last_delivery_complete == {"conv-fences": "task-2999"}
        old, new = asyncio.get_running_loop().create_future(), asyncio.get_running_loop().create_future()
        old.set_result(None)
        manager.register(conversation_id="conv-fences", task=old, task_id="old", cancel_event=cancel, active_conversation_id=None)
        manager.mark_delivery_complete("conv-fences", "old")
        manager.register(conversation_id="conv-fences", task=new, task_id="new", cancel_event=cancel, active_conversation_id=None)
        assert manager.is_delivery_complete("conv-fences", "old")
        assert not manager.is_delivery_complete("conv-fences", "new")
        manager.mark_delivery_complete("conv-fences", "new")
        assert manager.is_delivery_complete("conv-fences", "old")
        manager.cleanup(conversation_id="conv-fences", task=old, task_id="old", cancel_event=cancel)
        new.set_result(None)
        manager.cleanup(conversation_id="conv-fences", task=new, task_id="new", cancel_event=cancel)
        assert manager._delivery_complete == set()
        assert manager._last_delivery_complete == {"conv-fences": "new"}
        manager.mark_delivery_complete("conv-fences", "old")
        assert manager._last_delivery_complete == {"conv-fences": "new"}
        manager.cleanup(conversation_id="conv-fences", task=old, task_id="old", cancel_event=cancel)
        assert manager._delivery_complete == set()
        assert manager.is_delivery_complete("conv-fences", "new")
    finally:
        manager._unsubscribe_parent_notifications()


@pytest.mark.asyncio
async def test_new_run_registration_clears_both_legacy_delivery_markers():
    manager = SessionRunManager(SimpleNamespace(session_lifecycle=SimpleNamespace(schedule_task_runtime_update=lambda: None)))
    task = asyncio.get_running_loop().create_future()
    cancel = asyncio.Event()
    try:
        manager.mark_delivery_complete("conv-legacy", "")
        assert manager.is_delivery_complete("conv-legacy")
        manager.register(conversation_id="conv-legacy", task=task, task_id="new-run", cancel_event=cancel,
                         active_conversation_id=None)
        assert ("conv-legacy", "") not in manager._delivery_complete
        assert "conv-legacy" not in manager._last_delivery_complete
        assert not manager.is_delivery_complete("conv-legacy", "new-run")
        assert manager.running_task_for("conv-legacy") is task
    finally:
        task.set_result(None)
        manager.cleanup(conversation_id="conv-legacy", task=task, task_id="new-run", cancel_event=cancel)
        manager._unsubscribe_parent_notifications()


@pytest.mark.asyncio
async def test_repeated_restore_reuses_the_runtime_owned_journal_without_reparsing(tmp_path, monkeypatch):
    from backend.services.conversation_projection_service import recover_persisted_conversation_projections

    runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl", swarm_store_dir=tmp_path / "swarm",
                           enable_lease_heartbeat=False)
    repository = ConversationRepository(tmp_path / "conversations")
    conversation = repository.create_conversation()
    owner = execution_journal_owner("conversation", repository.store_instance_id(), conversation.id)
    journal = runtime.execution_journal(owner)
    journal.append_lifecycle("turn_started", {"conversation_id": conversation.id})
    def unexpected_read():
        raise AssertionError("unchanged warm journal must not be parsed again")
    def unexpected_constructor(*args, **kwargs):
        raise AssertionError("the runtime already owns this journal")
    monkeypatch.setattr(journal, "_read_events_unlocked", unexpected_read)
    monkeypatch.setattr(ExecutionJournal, "__init__", unexpected_constructor)
    try:
        await recover_persisted_conversation_projections(repository, conversation_id=conversation.id, runtime=runtime)
        await recover_persisted_conversation_projections(repository, conversation_id=conversation.id, runtime=runtime)
    finally:
        runtime.close(release_lease=True)


def test_settled_final_projection_does_not_query_sqlite_for_old_terminal_intents(tmp_path):
    reads = []
    def read_terminal(run_id):
        reads.append(run_id)
        return {"run_id": run_id, "status": "completed", "terminal_intent_event_id": current.event_id}
    journal = ExecutionJournal("settled-reader", base_dir=tmp_path / "sidechains", terminal_record_reader=read_terminal)
    def intent(run_id, message_id):
        return journal.append_lifecycle("terminal_intent", {
            "run_id": run_id, "conversation_id": "conv-settled", "message_id": message_id,
            "assistant_message": {"id": message_id, "role": "assistant", "content": "complete"},
            "context_snapshot": {"history": [{"role": "assistant", "content": "complete"}]},
        })
    for index in range(20):
        intent(f"old-{index}", f"answer-{index}")
        journal.append_lifecycle("conversation_projection_committed", {
            "run_id": f"old-{index}", "message_id": f"answer-{index}", "conversation_id": "conv-settled",
        })
    current = intent("current", "current-answer")
    projections = journal.unprojected_terminal_projections()
    assert reads == ["current"]
    assert projections[0]["source_event_id"] == current.event_id


@pytest.mark.asyncio
async def test_cold_projection_read_and_commit_receipt_run_off_the_event_loop(tmp_path, monkeypatch):
    from backend.services.conversation_projection_service import replay_pending_conversation_projections

    repository = ConversationRepository(tmp_path / "conversations")
    before = {"history": [{"role": "user", "content": "task"}], "turn_admissions": {
        "user": {"history_start": 0, "history_end": 1, "run_id": "run", "client_command_id": "command"},
    }}
    conversation = repository.create_conversation(transcript=[{"id": "user", "role": "user", "content": "task"}],
                                                context_snapshot=before)
    after = {**before, "history": [*before["history"], {"role": "assistant", "content": "complete"}]}
    journal = ExecutionJournal("cold-worker", base_dir=tmp_path / "sidechains")
    intent = journal.append_lifecycle("terminal_intent", {
        "conversation_id": conversation.id, "run_id": "run", "message_id": "answer",
        "assistant_message": {"id": "answer", "role": "assistant", "content": "complete", "terminal_status": "completed"},
        "context_snapshot": after, "context_delta": context_snapshot_delta(before, after), "source_user_message_ids": ["user"],
    })
    journal.append_lifecycle("runtime_terminal_committed", {"run_id": "run", "terminal_intent_event_id": intent.event_id})
    main_thread = threading.get_ident()
    observed = []
    read_events, append = journal.read_events, journal.append_lifecycle
    def worker_read():
        observed.append("read")
        assert threading.get_ident() != main_thread
        return read_events()
    def worker_receipt(*args, **kwargs):
        observed.append("receipt")
        assert threading.get_ident() != main_thread
        return append(*args, **kwargs)
    monkeypatch.setattr(journal, "read_events", worker_read)
    monkeypatch.setattr(journal, "append_lifecycle", worker_receipt)
    await replay_pending_conversation_projections(repository, journal, conversation_id=conversation.id)
    assert repository.get_conversation(conversation.id).transcript[-1]["content"] == "complete"
    assert "read" in observed and "receipt" in observed


if __name__ == "__main__":
    root = Path(sys.argv[2])
    if sys.argv[1] == "--terminal-child":
        asyncio.run(_terminal_child(root, sys.argv[3]))
    elif sys.argv[1] == "--create-child":
        asyncio.run(_create_child(root, sys.argv[3]))
