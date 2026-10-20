import asyncio
from contextlib import contextmanager
from datetime import UTC, datetime
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from backend.agent.conversation_query_guard import conversation_query_guards
from backend.conversations.repository import ConversationRepository
from backend.conversations.compression import compressed_history_path, read_history_bytes
from backend.services import general_maintenance as maintenance


NOW = datetime(2026, 10, 20, tzinfo=UTC)
OLD = datetime(2025, 1, 1, tzinfo=UTC)


def old_chat(repository, title, **fields):
    record = repository.create_conversation(title=title, transcript=[{
        "id": "user-original", "role": "user", "content": "Original text " * 5000,
        "timestamp": OLD.isoformat(),
    }], context_snapshot={"history": [{"role": "user", "content": "Original provider history"}], "pending_draft": "Keep me"})
    def age(chat):
        chat.updated_at = OLD.isoformat()
        for key, value in fields.items():
            setattr(chat, key, value)
    repository._mutate_meta(record.id, age, touch_updated_at=False)
    return repository.get_conversation(record.id)


def test_candidates_retain_recent_pinned_unread_running_scheduled_and_foreign_chats(tmp_path):
    repository = ConversationRepository(tmp_path / "conversations")
    eligible = old_chat(repository, "Eligible")
    pinned = old_chat(repository, "Pinned", pinned=True)
    unread = old_chat(repository, "Unread", has_unread_turn=True)
    active = old_chat(repository, "Active")
    archived = old_chat(repository, "Already archived", archived=True)
    foreign = old_chat(repository, "Foreign", thread_source="unknown")
    repository.create_conversation(title="Recent")
    candidates = maintenance.archive_candidates(repository, inactive_days=7, protected_ids={active.id}, now=NOW)
    assert [chat.id for chat in candidates] == [eligible.id]
    assert not maintenance.archive_candidates(repository, inactive_days=0, protected_ids=set(), now=NOW)
    assert not any(repository.get_conversation(chat.id).archived for chat in [eligible, pinned, unread, active, foreign])
    assert repository.get_conversation(archived.id).archived


def test_archiving_uses_the_real_query_claim_and_preserves_history_and_snapshots(tmp_path, monkeypatch):
    from backend.ws.handlers import conversation as handlers
    repository = ConversationRepository(tmp_path / "conversations")
    eligible = old_chat(repository, "Can archive")
    running = old_chat(repository, "Still running")
    session = SimpleNamespace(active_conversation_id="", conversation_repo=repository)
    manager = SimpleNamespace(resource_sessions=lambda: [session], execution_contexts=SimpleNamespace(context_for=lambda _id: None),
        unload_conversation_execution=AsyncMock(return_value=True))
    monkeypatch.setattr(maintenance, "conversation_activity_blockers", lambda *_: {})
    monkeypatch.setattr(maintenance, "_current_scheduler", lambda: None)
    broadcast = AsyncMock()
    monkeypatch.setattr(handlers, "_broadcast_conversation_lists", broadcast)
    forgotten = []
    monkeypatch.setattr(handlers, "_schedule_long_term_memory_forgetting", lambda _session, chat: forgotten.append(chat.id))
    claim = conversation_query_guards().try_start(running.id, owner_id="fixture-running-turn")
    try:
        result = asyncio.run(maintenance.maintain_local_chats(manager, repository=repository,
            auto_archive_after_days=7, compress_local_history=False, now=NOW))
    finally:
        conversation_query_guards().end(claim)
    assert result["archived_conversation_ids"] == [eligible.id]
    assert forgotten == [eligible.id]
    manager.unload_conversation_execution.assert_awaited_once_with(eligible.id, reason="conversation_archived")
    broadcast.assert_awaited_once_with(session)
    reloaded = ConversationRepository(repository.storage_root)
    archived = reloaded.get_conversation(eligible.id)
    assert archived.archived
    assert archived.transcript == eligible.transcript
    assert archived.context_snapshot == eligible.context_snapshot
    assert not reloaded.get_conversation(running.id).archived


def test_auto_archive_distinguishes_future_cron_heartbeat_targets_and_actual_scheduler_work(tmp_path, monkeypatch):
    from backend.tasks.scheduler import ScheduledTask, ScheduledTaskRun, TaskScheduler
    from backend.ws.handlers import conversation as handlers

    repository = ConversationRepository(tmp_path / "conversations")
    future = old_chat(repository, "Future cron may archive")
    heartbeat = old_chat(repository, "Heartbeat target must remain")
    pending = old_chat(repository, "Pending scheduled run")
    cleanup = old_chat(repository, "Scheduled worker cleanup")
    worker_chat = old_chat(repository, "Real worker still alive")
    scheduler = TaskScheduler.__new__(TaskScheduler)
    scheduler._tasks = {
        "future": ScheduledTask(id="future", kind="cron", conversation_id=future.id, enabled=True,
            next_run_at="2030-01-01T00:00:00+00:00"),
        "heartbeat": ScheduledTask(id="heartbeat", kind="heartbeat", conversation_id=heartbeat.id, enabled=False),
    }
    scheduler._runs = {
        "pending": ScheduledTaskRun(id="pending", task_id="other", conversation_id=pending.id, status="pending"),
        "cleanup": ScheduledTaskRun(id="cleanup", task_id="other", conversation_id=cleanup.id, status="completed", cleanup_pending=True),
        "worker": ScheduledTaskRun(id="worker", task_id="other", conversation_id=worker_chat.id, status="completed"),
    }
    worker = SimpleNamespace(done=lambda: False, cancel=Mock())
    scheduler._run_tasks = {"worker": worker}
    scheduler._unusable_schedule_ids = set()
    scheduler._save = Mock()
    session = SimpleNamespace(active_conversation_id="", conversation_repo=repository)
    manager = SimpleNamespace(resource_sessions=lambda: [session], execution_contexts=SimpleNamespace(context_for=lambda _id: None),
        unload_conversation_execution=AsyncMock(return_value=True))
    monkeypatch.setattr(maintenance, "_current_scheduler", lambda: scheduler)
    # The manual-archive blocker deliberately counts future schedules. This
    # value must not keep a quiet non-heartbeat chat forever out of archive.
    monkeypatch.setattr(maintenance, "conversation_activity_blockers", lambda *_: {"scheduled_tasks": 1})
    monkeypatch.setattr(handlers, "_broadcast_conversation_lists", AsyncMock())
    monkeypatch.setattr(handlers, "_schedule_long_term_memory_forgetting", lambda *_: None)
    result = asyncio.run(maintenance.maintain_local_chats(manager, repository=repository,
        auto_archive_after_days=7, compress_local_history=False, now=NOW))
    assert result["archived_conversation_ids"] == [future.id]
    assert scheduler._tasks["future"].enabled is False
    assert scheduler._tasks["future"].next_run_at is None
    scheduler._save.assert_called_once()
    assert not any(repository.get_conversation(chat.id).archived for chat in [heartbeat, pending, cleanup, worker_chat])
    assert scheduler._runs["pending"].status == "pending"
    assert scheduler._runs["cleanup"].cleanup_pending
    worker.cancel.assert_not_called()
    manager.unload_conversation_execution.assert_awaited_once_with(future.id, reason="conversation_archived")


def test_unload_failure_retains_the_unarchived_chat_and_propagates_the_real_failure(tmp_path, monkeypatch):
    repository = ConversationRepository(tmp_path / "conversations")
    original = old_chat(repository, "Do not archive an unreleased core")
    session = SimpleNamespace(active_conversation_id="", conversation_repo=repository)
    manager = SimpleNamespace(resource_sessions=lambda: [session], execution_contexts=SimpleNamespace(context_for=lambda _id: None),
        unload_conversation_execution=AsyncMock(return_value=False))
    monkeypatch.setattr(maintenance, "_current_scheduler", lambda: None)
    monkeypatch.setattr(maintenance, "conversation_activity_blockers", lambda *_: {})
    with pytest.raises(RuntimeError, match="执行环境尚未释放"):
        asyncio.run(maintenance.maintain_local_chats(manager, repository=repository,
            auto_archive_after_days=7, compress_local_history=False, now=NOW))
    retained = repository.get_conversation(original.id)
    assert not retained.archived
    assert retained.transcript == original.transcript
    assert retained.context_snapshot == original.context_snapshot
    assert conversation_query_guards().active_claim(original.id) is None


def test_auto_archive_unloads_the_real_idle_core_and_notification_listener_without_purging_user_data(tmp_path, monkeypatch):
    from backend.agent.message import UserCommand
    from backend.agent.parent_notification_outbox import (
        ParentNotificationOutbox, claim_parent_notification_wake, release_parent_notification_wake,
    )
    from backend.agent import runtime as runtime_module
    from backend.preview import launcher
    from backend.ws.handlers import conversation as handlers
    from backend.ws.manager import WebSocketManager
    from backend.ws.run_manager import SessionRunManager
    from backend.ws.session_lifecycle import SessionLifecycle

    repository = ConversationRepository(tmp_path / "conversations")
    eligible = old_chat(repository, "Unload the real retained core")
    other = old_chat(repository, "Keep another chat's queued work", pinned=True)
    outbox = ParentNotificationOutbox(conversation_id=eligible.id, base_dir=tmp_path / "parent-notifications")
    outbox.enqueue(subagent_id="fixture-finished-child", payload={"result": "Original durable evidence"})
    outbox_before = outbox.path.read_bytes()
    monkeypatch.setattr(runtime_module, "default_runtime_if_initialized", lambda: None)
    monkeypatch.setattr(launcher, "stop_preview_launches_for_conversation", AsyncMock())
    monkeypatch.setattr(maintenance, "_current_scheduler", lambda: None)
    monkeypatch.setattr(maintenance, "conversation_activity_blockers", lambda *_: {})
    monkeypatch.setattr(handlers, "_broadcast_conversation_lists", AsyncMock())
    monkeypatch.setattr(handlers, "_schedule_long_term_memory_forgetting", lambda *_: None)

    class FixtureSession:
        pass

    async def scenario():
        session = FixtureSession()
        session.session_id = "fixture-maintenance-core"
        session.window_instance_id = "fixture-window"
        session.window_owner_id = "fixture-owner"
        session.active_conversation_id = ""
        session.conversation_repo = repository
        session.cleanup_tasks = set()
        session.cancel_pending_approvals = AsyncMock()
        session.background_manager = SimpleNamespace(destroy_for_conversation=AsyncMock())
        session.terminal_manager = SimpleNamespace(destroy_sessions_for_conversation=AsyncMock())
        session.conversation_runtime = SimpleNamespace(forget_code_store=Mock())
        manager = WebSocketManager()
        session.ws_manager = manager
        session.session_lifecycle = SessionLifecycle(session)
        session.session_lifecycle.schedule_task_runtime_update = Mock()
        session.run_manager = SessionRunManager(session)
        session.cancel_agent_runs = session.run_manager.cancel
        manager._sessions[session.session_id] = session
        session.run_manager.enqueue_user_message(other.id, UserCommand(type="user_message", data={"content": "ORIGINAL QUEUED FOLLOW-UP"}))
        original_queue = session.run_manager.queued_user_message_snapshot(other.id)
        # An idle loaded listener has no live wake task but still owns its
        # process reservation; the actual unload must release both bindings.
        session.run_manager._watched_notification_conversations.add(eligible.id)
        assert claim_parent_notification_wake(eligible.id, session.run_manager._notification_wake_owner_token)
        context = manager.execution_contexts.load(eligible.id, session,
            has_subscribers=False, is_active=False, now=0)
        async def close_environment():
            assert not repository.get_conversation_summary(eligible.id).archived
        environment = SimpleNamespace(aclose=AsyncMock(side_effect=close_environment))
        context.thread_runtime.thread_environments = environment
        try:
            result = await maintenance.maintain_local_chats(manager, repository=repository,
                auto_archive_after_days=7, compress_local_history=False, now=NOW)
            assert result["archived_conversation_ids"] == [eligible.id]
            assert manager.execution_contexts.context_for(eligible.id) is None
            environment.aclose.assert_awaited_once()
            session.conversation_runtime.forget_code_store.assert_called_once_with(eligible.id)
            assert eligible.id not in session.run_manager._watched_notification_conversations
            assert claim_parent_notification_wake(eligible.id, "fixture-next-owner")
            assert session.run_manager.queued_user_message_snapshot(other.id) == original_queue
            assert outbox.path.read_bytes() == outbox_before
        finally:
            release_parent_notification_wake(eligible.id, "fixture-next-owner")
            release_parent_notification_wake(eligible.id, session.run_manager._notification_wake_owner_token)
            session.run_manager.stop_notification_wake_intake()
            session.run_manager.close_durable_queue()
    asyncio.run(scenario())


def test_cold_selection_only_returns_existing_seven_day_history_files(tmp_path):
    repository = ConversationRepository(tmp_path / "conversations")
    cold = old_chat(repository, "Cold")
    protected = old_chat(repository, "Protected")
    recent = repository.create_conversation(title="Recent")
    for file in repository.storage_root.iterdir():
        if not file.name.startswith(recent.id + "."):
            os.utime(file, (OLD.timestamp(), OLD.timestamp()))
    files = maintenance.cold_history_files(repository, protected_ids={protected.id}, now=NOW)
    assert files
    assert all(file.name.startswith(cold.id + ".") for file in files)
    assert not any(file.name.endswith((".meta.json", ".manifest.json")) for file in files)
    assert repository.get_conversation(recent.id) is not None


def test_cold_selection_finishes_file_stats_before_a_checkpoint_reclaims_its_previous_generation(tmp_path, monkeypatch):
    repository = ConversationRepository(tmp_path / "conversations")
    record = old_chat(repository, "Cold generation")
    repository.append_transcript_message(record.id, {"id": "second", "role": "user", "content": "Second checkpoint"})
    files = repository.history_paths(record.id)
    retired = next(path for path in files if ".g1.transcript.jsonl" in path.name)
    for path in files:
        os.utime(path, (OLD.timestamp(), OLD.timestamp()))
    owner_lock, is_file = repository._store_lock, Path.is_file
    owner_depth = 0
    checkpoint_waiting = checkpoint_finished = probed = False

    def finish_checkpoint():
        nonlocal checkpoint_waiting, checkpoint_finished
        checkpoint_waiting = False
        repository.append_transcript_message(record.id, {"id": "third", "role": "user", "content": "Concurrent checkpoint"})
        checkpoint_finished = True

    @contextmanager
    def writer_waits_for_reader_owner(*owners):
        nonlocal owner_depth
        if owners != (record.id,):
            with owner_lock(*owners):
                yield
            return
        with owner_lock(*owners):
            owner_depth += 1
            try:
                yield
            finally:
                owner_depth -= 1
        if not owner_depth and checkpoint_waiting:
            finish_checkpoint()

    def checkpoint_requested_after_file_probe(path):
        nonlocal checkpoint_waiting, probed
        exists = is_file(path)
        if path == retired and not probed:
            probed = checkpoint_waiting = True
            # A real third commit removes g1. It can finish immediately if the
            # scan released ownership after collecting the history paths.
            if not owner_depth:
                finish_checkpoint()
        return exists

    monkeypatch.setattr(repository, "_store_lock", writer_waits_for_reader_owner)
    monkeypatch.setattr(Path, "is_file", checkpoint_requested_after_file_probe)
    selected = maintenance.cold_history_files(repository, protected_ids=set(), now=NOW)
    assert probed and checkpoint_finished and not retired.exists()
    assert retired in selected
    assert maintenance.compress_cold_history(repository, selected, now=NOW) == 2
    restored = ConversationRepository(repository.storage_root).get_conversation(record.id)
    assert [message["id"] for message in restored.transcript] == ["user-original", "second", "third"]
    assert restored.context_snapshot == record.context_snapshot


def test_zstd_fixture_compression_keeps_exact_bytes_index_reads_and_resume_writes(tmp_path):
    repository = ConversationRepository(tmp_path / "conversations")
    original = old_chat(repository, "Cold local history")
    draft_file = tmp_path / "unsent-draft.json"
    draft_file.write_text('{"draft":"ORIGINAL UNSENT DRAFT"}', encoding="utf8")
    for file in repository.storage_root.iterdir():
        os.utime(file, (OLD.timestamp(), OLD.timestamp()))
    files = maintenance.cold_history_files(repository, protected_ids=set(), now=NOW)
    before = {file: file.read_bytes() for file in files}
    assert maintenance.compress_cold_history(repository, files, now=NOW) == len(files)
    assert all(not file.exists() and compressed_history_path(file).exists() for file in files)
    assert {file: read_history_bytes(file) for file in files} == before
    fresh = ConversationRepository(repository.storage_root)
    assert fresh.get_conversation(original.id).context_snapshot == original.context_snapshot
    page = fresh.get_transcript_page(original.id, limit=20)
    assert page["transcript"] == original.transcript
    fresh.rename_conversation(original.id, "Resumed after compression")
    resumed = fresh.get_conversation(original.id)
    assert resumed.title == "Resumed after compression"
    assert resumed.transcript == original.transcript
    assert draft_file.read_text(encoding="utf8") == '{"draft":"ORIGINAL UNSENT DRAFT"}'
