from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.agent.message import AgentEvent, UserCommand
from backend.api import _state
from backend.conversations.repository import ConversationRepository
from backend.main import app
from backend.ws.durable_user_queue import DurableUserMessageQueue
from backend.ws.run_manager import SessionRunManager
from tests.test_conversations import _install_noop_llm, _receive_until_event


def test_legacy_migration_retains_identical_inputs_and_settles_mutated_claims(tmp_path):
    repository = ConversationRepository(tmp_path / "conversations")
    legacy = DurableUserMessageQueue(session_id="old-window", root_dir=tmp_path / "user-message-queue")
    attachment = {"file_name": "notes.txt", "artifact_id": "original-attachment"}
    try:
        legacy.save({"conversation": [
            UserCommand("user_message", {"content": "same input", "attachments": [attachment]}),
            UserCommand("user_message", {"content": "same input", "attachments": [attachment]}),
            UserCommand("user_message", {"content": "old client input", "client_command_id": "old-command",
                                         "_legacy_user_message_identity": True}),
        ]}, {})
    finally:
        legacy.close()

    manager = SessionRunManager(SimpleNamespace(session_id="new-window", conversation_repo=repository))
    try:
        pending = manager.queued_user_messages("conversation")
        assert [item.data["content"] for item in pending] == ["same input", "same input", "old client input"]
        assert all(item.data["attachments"] == [attachment] for item in pending[:2])
        assert pending[0].data["user_message_id"] != pending[1].data["user_message_id"]
        assert pending[0].data["assistant_message_id"] != pending[1].data["assistant_message_id"]
        assert "user_message_id" not in pending[2].data
        assert pending[2].data["_legacy_user_message_identity"] is True

        for index in range(3):
            command = manager.dequeue_user_message("conversation")
            command.data["_queued_user_message_dispatch"] = True
            if index == 2:
                command.data.update(user_message_id="recovered-user", assistant_message_id="recovered-answer")
            manager.finish_user_message_dispatch("conversation", command, succeeded=True)
            assert not manager.has_inflight_user_message("conversation")
        assert not manager.queued_user_messages("conversation")
    finally:
        manager._unsubscribe_parent_notifications()
        manager.close_durable_queue()


def test_legacy_identity_and_input_survive_a_process_exit_after_claim(tmp_path):
    program = r'''
import json,os,sys
from pathlib import Path
from backend.agent.message import UserCommand
from backend.ws.durable_user_queue import DurableUserMessageQueue
queue = DurableUserMessageQueue(session_id="shared", root_dir=Path(sys.argv[1]), shared_inputs=True)
queue.save({"conversation": [UserCommand("user_message", {"content": "recover original input"})]}, {})
claimed = queue.claim_user_message("conversation")
print(json.dumps({"user": claimed.data["user_message_id"], "assistant": claimed.data["assistant_message_id"]}), flush=True)
os._exit(83)
'''
    root = tmp_path / "queue"
    process = subprocess.run([sys.executable, "-c", program, str(root)], cwd=Path(__file__).resolve().parents[2],
                             capture_output=True, text=True, timeout=30)
    assert process.returncode == 83, process.stderr
    identity = json.loads(process.stdout)
    queue = DurableUserMessageQueue(session_id="shared", root_dir=root, shared_inputs=True)
    try:
        recovered = queue.claim_user_message("conversation")
        assert recovered.data["content"] == "recover original input"
        assert recovered.data["user_message_id"] == identity["user"]
        assert recovered.data["assistant_message_id"] == identity["assistant"]
        recovered.data["_queued_user_message_dispatch"] = True
        assert queue.settle_user_message("conversation", recovered, succeeded=True)
        assert not queue.pending_user_messages("conversation")
    finally:
        queue.close()


@pytest.mark.timeout(45)
def test_restored_legacy_inputs_reach_the_runner_and_websocket_in_order(tmp_path, monkeypatch):
    _install_noop_llm(monkeypatch)
    conversation_dir = tmp_path / "conversations"
    monkeypatch.setattr("backend.main.CONVERSATION_DATA_DIR", conversation_dir, raising=False)
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", conversation_dir, raising=False)
    repository = ConversationRepository(conversation_dir)
    owner = repository.create_conversation(title="Legacy queued inputs", permission_mode="bypass", memory_mode="none")
    legacy = DurableUserMessageQueue(session_id="old-window", root_dir=tmp_path / "user-message-queue")
    try:
        legacy.save({owner.id: [UserCommand("user_message", {"content": "same queued input", "conversation_id": owner.id}),
                                UserCommand("user_message", {"content": "same queued input", "conversation_id": owner.id})]}, {})
    finally:
        legacy.close()

    ran, finished, events = [], threading.Event(), []
    with TestClient(app) as client:
        with client.websocket_connect("/ws?session_id=legacy-input-live-test") as websocket:
            _receive_until_event(websocket, "llm.model.updated")
            session = _state.ws_manager.get_session("legacy-input-live-test")
            send_event = session.send_event

            async def capture(event):
                await send_event(event)
                if event.type in {"error", "command.result", "done"}:
                    events.append((event.type, dict(event.data)))
                if event.type == "done" and len(ran) == 2:
                    finished.set()

            async def simulated_run(content, *, conversation_id, metadata, **_kwargs):
                ran.append((content, metadata["user_message_id"], metadata["assistant_message_id"]))
                metadata["_turn_admitted"] = True
                metadata["_turn_admission_future"].set_result(None)
                done = AgentEvent.done(status="completed", reason="completed")
                done.data.update(conversation_id=conversation_id, message_id=metadata["assistant_message_id"])
                await session.send_event(done)
                session.run_manager.mark_terminal_status(conversation_id, "completed")
                session.run_manager.mark_delivery_complete(conversation_id, metadata["_run_task_id"])

            monkeypatch.setattr(session, "send_event", capture)
            monkeypatch.setattr(session, "_run_agent", simulated_run)
            websocket.send_json({"type": "session.restore", "last_conversation_id": owner.id})
            assert finished.wait(15), events
            assert [item[0] for item in ran] == ["same queued input", "same queued input"]
            assert len({item[1] for item in ran}) == len({item[2] for item in ran}) == 2
            delivered = []
            while len(delivered) < 2:
                payload = websocket.receive_json()
                if payload.get("type") == "done" and payload.get("conversation_id") == owner.id:
                    delivered.append(payload["message_id"])
            assert delivered == [item[2] for item in ran]
            deadline = time.monotonic() + 5
            while session.run_manager.has_inflight_user_message(owner.id) and time.monotonic() < deadline:
                time.sleep(.02)
            assert not session.run_manager.has_inflight_user_message(owner.id)
            assert not session.run_manager.queued_user_messages(owner.id)


def test_legacy_migration_keeps_its_receipt_after_source_clear_fails(tmp_path, monkeypatch):
    source = DurableUserMessageQueue(session_id="old-window", root_dir=tmp_path / "source")
    destination = DurableUserMessageQueue(session_id="shared", root_dir=tmp_path / "destination", shared_inputs=True)
    try:
        source.save({"conversation": [UserCommand("user_message", {"content": "same"}),
                                       UserCommand("user_message", {"content": "same"})]}, {})
        with monkeypatch.context() as fault:
            def fail_source_clear():
                raise OSError("source clear failed")
            fault.setattr(source, "_write_current_unlocked", fail_source_clear)
            try:
                source.migrate_user_messages_to(destination)
            except OSError as exc:
                assert str(exc) == "source clear failed"
            else:
                raise AssertionError("source clear should fail")
        pending = destination.pending_user_messages("conversation")
        assert len(pending) == 2 and pending[0].data["user_message_id"] != pending[1].data["user_message_id"]
        assert source.migrate_user_messages_to(destination) == 0
        assert len(destination.pending_user_messages("conversation")) == 2
        assert not source.pending_user_messages("conversation")
    finally:
        source.close()
        destination.close()
