from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.agent.message import UserCommand
from backend.api import _state
from backend.main import app
from backend.tests.test_conversation_window_follower import windows
from backend.tests.test_steer_expected_turn_chain import queued
from backend.ws.durable_user_queue import DurableUserMessageQueue
from backend.ws.stream_state import create_stream_state
from tests.test_conversations import _install_noop_llm, _receive_until_event


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["delete", "edit"])
async def test_other_process_cannot_change_selected_queue_input_before_steer(windows, monkeypatch, change):
    _manager, owner, _follower, conversation, _other, start, drain = windows
    active = start()
    original = queued(owner, conversation, "selected")
    enqueue = owner.run_manager.enqueue_turn_steer
    child = r'''
import json,sys
from pathlib import Path
from types import SimpleNamespace
from backend.conversations.repository import ConversationRepository
from backend.ws.run_manager import SessionRunManager
repo=ConversationRepository(Path(sys.argv[1]))
manager=SessionRunManager(SimpleNamespace(session_id="external-window",conversation_repo=repo))
manager.stop_notification_wake_intake()
if sys.argv[4] == "delete":
    result=manager.remove_queued_user_message(sys.argv[2],sys.argv[3])
else:
    result=manager.update_queued_user_message(sys.argv[2],sys.argv[3],{"content":"new user edit","attachments":[]}) is not None
print(json.dumps({"changed": bool(result)}),flush=True)
manager.close_durable_queue()
'''
    external = []

    def interleave_external_change(*args, **kwargs):
        process = subprocess.run([sys.executable, "-c", child, str(owner.conversation_repo._base_dir),
                                  conversation.id, "selected", change], cwd=Path(__file__).resolve().parents[2],
                                 capture_output=True, text=True, timeout=25)
        external.append((process.returncode, process.stdout, process.stderr))
        return enqueue(*args, **kwargs)

    monkeypatch.setattr(owner.run_manager, "enqueue_turn_steer", interleave_external_change)
    await owner.command_dispatcher._dispatch_client_command(UserCommand("user_message.queue.steer", {
        "client_command_id": "steer-after-change", "conversation_id": conversation.id,
        "message_id": "selected", "expected_turn_id": "actual-turn",
    }), owner.connection_generation)
    await drain()
    assert external[0][0] == 0, external[0][2]
    assert json.loads(external[0][1])["changed"] is True
    result = next(item for item in owner.ws.sent if item.get("client_command_id") == "steer-after-change"
                  and item.get("type") == "command.result")
    assert result["level"] == "error" and result["data"]["reason"] == "queue_message_changed"
    assert result["data"]["queued"] is (change == "edit")
    assert not owner.run_manager.turn_input_queue(conversation.id).snapshot()
    assert not owner.run_manager._durable_turn_inputs.get(conversation.id)
    pending = owner.run_manager.queued_user_messages(conversation.id)
    if change == "edit":
        assert [item.data["content"] for item in pending] == ["new user edit"]
        update = next(item for item in owner.ws.sent if item.get("type") == "user_message.queue.updated"
                      and item.get("message_id") == "selected" and item.get("input"))
        assert update["input"]["content"] == "new user edit"
    else:
        assert pending == []
    assert owner.run_manager.running_task_for(conversation.id) is active
    assert original.data["content"] == "Original queued input"


@pytest.mark.timeout(45)
def test_websocket_steer_reprojects_an_external_edit_without_admitting_old_input(tmp_path, monkeypatch):
    import asyncio

    _install_noop_llm(monkeypatch)
    monkeypatch.setattr("backend.main.CONVERSATION_DATA_DIR", tmp_path / "conversations", raising=False)
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", tmp_path / "conversations", raising=False)
    child = r'''
import sys
from pathlib import Path
from types import SimpleNamespace
from backend.conversations.repository import ConversationRepository
from backend.ws.run_manager import SessionRunManager
manager=SessionRunManager(SimpleNamespace(session_id="external-edit",conversation_repo=ConversationRepository(Path(sys.argv[1]))))
manager.stop_notification_wake_intake()
assert manager.update_queued_user_message(sys.argv[2],"queued-answer",{"content":"edited elsewhere","attachments":[]}) is not None
manager.close_durable_queue()
'''
    with TestClient(app) as client:
        with client.websocket_connect("/ws?session_id=steer-atomic-live") as websocket:
            _receive_until_event(websocket, "llm.model.updated")
            session = _state.ws_manager.get_session("steer-atomic-live")
            owner = session.conversation_repo.create_conversation(title="Steer race", memory_mode="none")
            session.active_conversation_id = owner.id

            async def seed_active_turn():
                active = asyncio.create_task(asyncio.Event().wait())
                session._register_agent_run(conversation_id=owner.id, task=active, task_id="active-task",
                    cancel_event=asyncio.Event())
                session._conversation_streams[owner.id] = create_stream_state(owner.id, "active-answer", "active-turn")
                session.run_manager.turn_input_queue(owner.id).begin_turn("active-turn")
                session.run_manager.enqueue_user_message(owner.id, UserCommand("user_message", {
                    "conversation_id": owner.id, "content": "original queued input",
                    "user_message_id": "queued-user", "assistant_message_id": "queued-answer",
                }))

            client.portal.call(seed_active_turn)
            enqueue = session.run_manager.enqueue_turn_steer
            external = []

            def interleave_external_edit(*args, **kwargs):
                process = subprocess.run([sys.executable, "-c", child, str(session.conversation_repo._base_dir), owner.id],
                                         cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, timeout=25)
                external.append((process.returncode, process.stderr))
                return enqueue(*args, **kwargs)

            monkeypatch.setattr(session.run_manager, "enqueue_turn_steer", interleave_external_edit)
            websocket.send_json({"type": "user_message.queue.steer", "conversation_id": owner.id,
                                 "message_id": "queued-answer", "expected_turn_id": "active-turn",
                                 "client_command_id": "stale-steer"})
            updated, result = None, None
            while result is None:
                payload = websocket.receive_json()
                if payload.get("type") == "user_message.queue.updated" and payload.get("input"):
                    updated = payload
                if payload.get("type") == "command.result" and payload.get("client_command_id") == "stale-steer":
                    result = payload
            assert external[0][0] == 0, external[0][1]
            assert result["level"] == "error" and result["data"]["reason"] == "queue_message_changed"
            assert result["data"]["queued"] is True
            assert updated["input"]["content"] == "edited elsewhere"
            assert not session.run_manager.turn_input_queue(owner.id).snapshot()
            assert [item.data["content"] for item in session.run_manager.queued_user_messages(owner.id)] == ["edited elsewhere"]


def test_promoted_input_survives_process_exit_before_live_publish(tmp_path):
    program = r'''
import json,os,sys
from pathlib import Path
from backend.agent.message import UserCommand
from backend.ws.durable_user_queue import DurableUserMessageQueue
queue=DurableUserMessageQueue(session_id="shared",root_dir=Path(sys.argv[1]),shared_inputs=True)
command=UserCommand("user_message",{"conversation_id":"conv_owned","content":"retain steer", "user_message_id":"user-one","assistant_message_id":"answer-one"})
queue.save({"conv_owned":[command]}, {})
result=queue.promote_queued_user_message("conv_owned","answer-one",expected_turn_id="turn-one",expected_command=command)
assert result is not None
os._exit(94)
'''
    root = tmp_path / "queue"
    process = subprocess.run([sys.executable, "-c", program, str(root)], cwd=Path(__file__).resolve().parents[2],
                             capture_output=True, timeout=30)
    assert process.returncode == 94, process.stderr
    queue = DurableUserMessageQueue(session_id="shared", root_dir=root, shared_inputs=True)
    try:
        pending = queue.pending_user_messages("conv_owned")
        assert len(pending) == 1 and pending[0].data["content"] == "retain steer"
        assert pending[0].data["expected_turn_id"] == "turn-one"
        assert pending[0].data["assistant_message_id"] == "answer-one"
    finally:
        queue.close()


def test_failed_atomic_promotion_preserves_original_queue(tmp_path, monkeypatch):
    queue = DurableUserMessageQueue(session_id="shared", root_dir=tmp_path, shared_inputs=True)
    command = UserCommand("user_message", {"content": "keep input", "user_message_id": "user-one", "assistant_message_id": "answer-one"})
    try:
        queue.save({"conv_owned": [command]}, {})
        with monkeypatch.context() as fault:
            def fail_write():
                raise OSError("controlled promotion failure")
            fault.setattr(queue, "_write_current_unlocked", fail_write)
            with pytest.raises(OSError, match="controlled promotion failure"):
                queue.promote_queued_user_message("conv_owned", "answer-one", expected_turn_id="turn-one", expected_command=command)
        assert [item.data["content"] for item in queue.pending_user_messages("conv_owned")] == ["keep input"]
        assert not queue._turn_inputs.get("conv_owned")
    finally:
        queue.close()
