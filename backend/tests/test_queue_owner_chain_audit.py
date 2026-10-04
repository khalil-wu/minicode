from __future__ import annotations

import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from backend.agent.message import UserCommand
from backend.conversations.repository import ConversationRepository
from backend.ws.durable_user_queue import DurableUserMessageQueue
from backend.ws.run_manager import SessionRunManager


def _message(name):
    return UserCommand(type="user_message", data={
        "conversation_id": "conv_queueaudit", "assistant_message_id": name,
        "content": name,
    })


@contextmanager
def _manager(repository, session_id):
    manager = SessionRunManager(SimpleNamespace(session_id=session_id, conversation_repo=repository))
    try:
        yield manager
    finally:
        manager._unsubscribe_parent_notifications()
        manager.close_durable_queue()


def test_new_renderer_shares_pending_turns_and_keeps_window_commands_private(tmp_path):
    repository = ConversationRepository(tmp_path / "conversations")
    with _manager(repository, "session_first") as first:
        message = _message("first")
        message.data["_model_runtime_handle"] = object()
        first.enqueue_user_message("conv_queueaudit", message)
        first.durable_client_commands.persist_client_command(UserCommand(
            type="conversation.list", data={"client_command_id": "cmd_firstwindow"},
        ))
        with _manager(repository, "session_second") as second:
            assert [item.data["content"] for item in second.queued_user_messages("conv_queueaudit")] == ["first"]
            assert not second.durable_client_commands.pending_client_commands()
            assert first.queued_user_messages("conv_queueaudit")[0] is message
            assert first.dequeue_user_message("conv_queueaudit") is message
            assert second.dequeue_user_message("conv_queueaudit") is None
            first.finish_user_message_dispatch("conv_queueaudit", message, succeeded=True)
            assert not second.queued_user_message_snapshot()


def test_shared_queue_refreshes_cross_window_enqueue_and_cancel_without_lost_inputs(tmp_path):
    repository = ConversationRepository(tmp_path / "conversations")
    with _manager(repository, "session_first") as first, _manager(repository, "session_second") as second:
        first.enqueue_user_message("conv_queueaudit", _message("first"))
        second.enqueue_user_message("conv_queueaudit", _message("second"))
        first.enqueue_user_message("conv_queueaudit", _message("third"))
        assert [row["content"] for row in second.queued_user_message_snapshot()] == ["first", "second", "third"]
        assert first.remove_queued_user_message("conv_queueaudit", "second")
        assert [item.data["content"] for item in second.queued_user_messages("conv_queueaudit")] == ["first", "third"]
        second.clear_user_message_queue("conv_queueaudit")
        assert not first.queued_user_message_snapshot()


def test_new_renderer_recovers_a_closed_owners_inflight_turn(tmp_path):
    repository = ConversationRepository(tmp_path / "conversations")
    with _manager(repository, "session_first") as first:
        first.enqueue_user_message("conv_queueaudit", _message("first"))
        assert first.dequeue_user_message("conv_queueaudit") is not None
    with _manager(repository, "session_second") as second:
        assert second.dequeue_user_message("conv_queueaudit").data["content"] == "first"


def test_legacy_queue_migration_recovers_inflight_and_retains_client_commands(tmp_path):
    repository = ConversationRepository(tmp_path / "conversations")
    queue_root = tmp_path / "user-message-queue"
    legacy = DurableUserMessageQueue(session_id="session_legacy", root_dir=queue_root)
    legacy.save({}, {"conv_queueaudit": _message("legacy")})
    legacy.persist_client_command(UserCommand(type="conversation.list", data={"client_command_id": "cmd_legacywindow"}))
    legacy.close()
    with _manager(repository, "session_new") as manager:
        assert [row["content"] for row in manager.queued_user_message_snapshot()] == ["legacy"]
        assert not manager.durable_client_commands.pending_client_commands()
        old = json.loads(legacy.path.read_text(encoding="utf-8"))
        assert not old["queues"] and not old["inflight"]
        assert old["client_pending"][0]["data"]["client_command_id"] == "cmd_legacywindow"


def test_live_legacy_owner_is_preserved_then_adopted_when_it_closes(tmp_path):
    repository = ConversationRepository(tmp_path / "conversations")
    legacy = DurableUserMessageQueue(session_id="session_legacy", root_dir=tmp_path / "user-message-queue")
    try:
        legacy.save({"conv_queueaudit": [_message("legacy")]}, {})
        with _manager(repository, "session_new") as manager:
            assert not manager.queued_user_message_snapshot()
            assert legacy.pending_user_messages("conv_queueaudit")
            legacy.close()
            assert [row["content"] for row in manager.queued_user_message_snapshot()] == ["legacy"]
    finally:
        legacy.close()


def test_migration_receipt_prevents_reexecution_after_source_clear_failure(tmp_path, monkeypatch):
    source = DurableUserMessageQueue(session_id="session_legacy", root_dir=tmp_path / "legacy")
    destination = DurableUserMessageQueue(session_id="shared", root_dir=tmp_path / "shared")
    try:
        source.save({"conv_queueaudit": [_message("legacy")]}, {})
        original_write = source._write_current_unlocked

        def fail_source_clear():
            raise OSError("source clear failed")

        monkeypatch.setattr(source, "_write_current_unlocked", fail_source_clear)
        with pytest.raises(OSError, match="source clear failed"):
            source.migrate_user_messages_to(destination)
        admitted = destination.claim_user_message("conv_queueaudit")
        assert admitted is not None
        assert destination.settle_user_message("conv_queueaudit", admitted, succeeded=True)
        monkeypatch.setattr(source, "_write_current_unlocked", original_write)
        assert source.migrate_user_messages_to(destination) == 0
        assert not destination.pending_user_message_snapshot()
        assert not source.pending_user_message_snapshot()
    finally:
        source.close()
        destination.close()


def test_corrupt_legacy_source_keeps_its_evidence_visible_in_shared_store(tmp_path):
    repository = ConversationRepository(tmp_path / "conversations")
    source = DurableUserMessageQueue(session_id="session_legacy", root_dir=tmp_path / "user-message-queue")
    source.path.write_text("{broken", encoding="utf-8")
    source.close()
    with _manager(repository, "session_new") as manager:
        assert not manager.queued_user_message_snapshot()
        assert manager.durable_queue.load_error["reason"] == "malformed_json"
        assert list(source.path.parent.glob("session_legacy.json.corrupt-*"))
