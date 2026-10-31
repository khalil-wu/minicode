from __future__ import annotations

import asyncio
from pathlib import Path
import threading
import time
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from backend.agent.message import AgentEvent, UserCommand
from backend.api import _state
from backend.main import app
from tests.test_conversations import _install_noop_llm, _receive_until_event
from tests.test_ws_terminal_fallback import _session


@pytest.fixture(autouse=True)
def trust_test_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr("backend.workspace.trust.is_workspace_trusted", lambda path: Path(path).is_relative_to(tmp_path))


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["claim", "remaining", "workspace", "track"])
async def test_pre_dispatch_failure_releases_fence_and_preserves_queued_input(tmp_path, monkeypatch, stage):
    session = _session(tmp_path, monkeypatch, "conv_queueowner")
    session.active_conversation_id = "conv_queueowner"
    manager = session.run_manager
    command = UserCommand("user_message", {"conversation_id": "conv_queueowner", "content": "preserve queued input",
        "user_message_id": "queued-user", "assistant_message_id": "queued-answer"})
    manager.enqueue_user_message("conv_queueowner", command)
    session.command_dispatcher._handle_command = AsyncMock(return_value=True)

    def fail(*_args, **_kwargs):
        raise OSError("controlled queue dispatch failure")

    try:
        with monkeypatch.context() as fault:
            if stage == "claim":
                fault.setattr(manager, "dequeue_user_message", fail)
            elif stage == "remaining":
                fault.setattr(manager.durable_queue, "pending_user_messages", fail)
            elif stage == "workspace":
                summary = session.conversation_repo.get_conversation_summary
                reads = 0

                def fail_after_workspace_check(*args, **kwargs):
                    nonlocal reads
                    reads += 1
                    if reads == 1:
                        return summary(*args, **kwargs)
                    fail()

                fault.setattr(session.conversation_repo, "get_conversation_summary", fail_after_workspace_check)
            else:
                fault.setattr(session.command_dispatcher, "track_execution_command_task", fail)
            with pytest.raises(OSError, match="controlled queue dispatch failure"):
                session.schedule_next_queued_user_message("conv_queueowner")

        await asyncio.sleep(0)
        assert not manager.is_queue_dispatching("conv_queueowner")
        assert not manager.has_inflight_user_message("conv_queueowner")
        assert [item.data["content"] for item in manager.queued_user_messages("conv_queueowner")] == ["preserve queued input"]
        session.command_dispatcher._handle_command.assert_not_awaited()

        session.schedule_next_queued_user_message("conv_queueowner")
        await asyncio.wait_for(asyncio.gather(*tuple(session.command_dispatcher.execution_command_tasks)), 10)
        session.command_dispatcher._handle_command.assert_awaited_once()
        assert not manager.is_queue_dispatching("conv_queueowner")
        assert not manager.has_inflight_user_message("conv_queueowner")
        assert not manager.queued_user_messages("conv_queueowner")
    finally:
        await session.session_lifecycle.shutdown(reason="queue_dispatch_test")


@pytest.mark.asyncio
async def test_empty_queue_configuration_failure_releases_dispatch_fence(tmp_path, monkeypatch):
    session = _session(tmp_path, monkeypatch, "conv_queueowner")
    try:
        with monkeypatch.context() as fault:
            def fail(*_args, **_kwargs):
                raise OSError("controlled configuration failure")
            fault.setattr("backend.config.load_config", fail)
            with pytest.raises(OSError, match="controlled configuration failure"):
                session.schedule_next_queued_user_message("conv_queueowner")
        assert not session.run_manager.is_queue_dispatching("conv_queueowner")
        session.schedule_next_queued_user_message("conv_queueowner")
        assert not session.run_manager.is_queue_dispatching("conv_queueowner")
    finally:
        await session.session_lifecycle.shutdown(reason="queue_dispatch_test")


@pytest.mark.timeout(45)
def test_websocket_restore_recovers_after_a_claim_read_failure(tmp_path, monkeypatch):
    _install_noop_llm(monkeypatch)
    monkeypatch.setattr("backend.main.CONVERSATION_DATA_DIR", tmp_path / "conversations", raising=False)
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", tmp_path / "conversations", raising=False)
    finished, calls = threading.Event(), []
    with TestClient(app) as client:
        with client.websocket_connect("/ws?session_id=queue-restore-fault-test") as websocket:
            _receive_until_event(websocket, "llm.model.updated")
            session = _state.ws_manager.get_session("queue-restore-fault-test")
            owner = session.conversation_repo.create_conversation(title="Queue restore", permission_mode="bypass", memory_mode="none")
            manager = session.run_manager
            manager.enqueue_user_message(owner.id, UserCommand("user_message", {"conversation_id": owner.id,
                "content": "retain this queued input", "user_message_id": "queued-user", "assistant_message_id": "queued-answer"}))
            dequeue = manager.dequeue_user_message
            failed = False

            def fail_once_after_claim(conversation_id):
                nonlocal failed
                if failed:
                    return dequeue(conversation_id)
                failed = True
                with monkeypatch.context() as fault:
                    def fail(*_args, **_kwargs):
                        raise OSError("controlled pending queue read failure")
                    fault.setattr(manager.durable_queue, "pending_user_messages", fail)
                    return dequeue(conversation_id)

            async def simulated_run(content, *, conversation_id, metadata, **_kwargs):
                calls.append(content)
                metadata["_turn_admitted"] = True
                metadata["_turn_admission_future"].set_result(None)
                done = AgentEvent.done(status="completed", reason="completed")
                done.data.update(conversation_id=conversation_id, message_id=metadata["assistant_message_id"])
                await session.send_event(done)
                manager.mark_terminal_status(conversation_id, "completed")
                manager.mark_delivery_complete(conversation_id, metadata["_run_task_id"])
                finished.set()

            monkeypatch.setattr(manager, "dequeue_user_message", fail_once_after_claim)
            monkeypatch.setattr(session, "_run_agent", simulated_run)
            websocket.send_json({"type": "session.restore", "last_conversation_id": owner.id})
            while True:
                payload = websocket.receive_json()
                if payload.get("type") == "command.result" and payload.get("command") == "session.restore":
                    break
            assert payload["level"] == "error" and "controlled pending queue read failure" in payload["message"]
            assert not manager.is_queue_dispatching(owner.id) and not manager.has_inflight_user_message(owner.id)
            assert [item.data["content"] for item in manager.queued_user_messages(owner.id)] == ["retain this queued input"]
            websocket.send_json({"type": "session.restore", "last_conversation_id": owner.id})
            assert finished.wait(15)
            done = _receive_until_event(websocket, "done")
            assert done["message_id"] == "queued-answer" and calls == ["retain this queued input"]
            deadline = time.monotonic() + 5
            while manager.has_inflight_user_message(owner.id) and time.monotonic() < deadline:
                time.sleep(.02)
            assert not manager.has_inflight_user_message(owner.id) and not manager.queued_user_messages(owner.id)
