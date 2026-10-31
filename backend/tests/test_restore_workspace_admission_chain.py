from __future__ import annotations

import asyncio
import threading
import time

import pytest
from fastapi.testclient import TestClient

from backend.agent.message import AgentEvent, UserCommand
from backend.api import _state
from backend.conversations.repository import ConversationRepository
from backend.main import app
from backend.tests.test_ws_cold_connection import _connection, _release_session
from backend.ws.durable_user_queue import DurableUserMessageQueue
from backend.ws.manager import WebSocketManager
from tests.test_conversations import _install_noop_llm, _receive_until_event


def _simulate_completed_run(calls, finished):
    async def run(session, content, *, conversation_id, metadata, **_kwargs):
        calls.append(content)
        metadata["_turn_admitted"] = True
        metadata["_turn_admission_future"].set_result(None)
        done = AgentEvent.done(status="completed", reason="completed")
        done.data.update(conversation_id=conversation_id, message_id=metadata["assistant_message_id"])
        await session.send_event(done)
        session.run_manager.mark_terminal_status(conversation_id, "completed")
        session.run_manager.mark_delivery_complete(conversation_id, metadata["_run_task_id"])
        finished.set()

    return run


def _receive_restored_with_workspace_error(websocket):
    events = []
    for _ in range(100):
        event = websocket.receive_json()
        events.append(event)
        if event.get("type") == "session.restored":
            assert any(item.get("command") == "workspace.activate" and item.get("level") == "error" for item in events)
            return event
    raise AssertionError("No session.restored event was received")


@pytest.mark.timeout(60)
@pytest.mark.parametrize("reopen", ["conversation.switch", "session.restore"])
def test_failed_restore_keeps_queued_input_until_workspace_reopens(tmp_path, monkeypatch, reopen):
    _install_noop_llm(monkeypatch)
    monkeypatch.setattr("backend.workspace.trust.is_workspace_trusted", lambda _path: True)
    conversation_dir = tmp_path / "conversations"
    monkeypatch.setattr("backend.main.CONVERSATION_DATA_DIR", conversation_dir, raising=False)
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", conversation_dir, raising=False)
    root = tmp_path / "missing-workspace"
    calls, finished = [], threading.Event()
    with TestClient(app) as client:
        with client.websocket_connect("/ws?session_id=missing-restore") as websocket:
            _receive_until_event(websocket, "llm.model.updated")
            session = _state.ws_manager.get_session("missing-restore")
            owner = session.conversation_repo.create_conversation(title="Missing workspace", workspace_root=str(root))
            manager = session.run_manager
            manager.enqueue_user_message(owner.id, UserCommand("user_message", {
                "conversation_id": owner.id, "content": "retained input",
                "user_message_id": "queued-user", "assistant_message_id": "queued-answer",
            }))
            monkeypatch.setattr(session, "_run_agent", _simulate_completed_run(calls, finished).__get__(session))

            websocket.send_json({"type": "session.restore", "last_conversation_id": owner.id})
            restored = _receive_restored_with_workspace_error(websocket)
            assert restored["error"] and not calls
            websocket.send_json({"type": "session.sync"})
            _receive_until_event(websocket, "session.synced", max_attempts=80)
            assert not calls
            assert [item.data["content"] for item in manager.queued_user_messages(owner.id)] == ["retained input"]

            root.mkdir()
            data = {"conversation_id": owner.id} if reopen == "conversation.switch" else {"last_conversation_id": owner.id}
            websocket.send_json({"type": reopen, **data})
            assert finished.wait(20)
            _receive_until_event(websocket, "done", max_attempts=100)
            deadline = time.monotonic() + 5
            while manager.has_inflight_user_message(owner.id) and time.monotonic() < deadline:
                time.sleep(.02)
            assert calls == ["retained input"]
            assert not manager.has_inflight_user_message(owner.id)
            assert not manager.queued_user_messages(owner.id)


@pytest.mark.timeout(60)
def test_cold_replay_keeps_input_for_missing_workspace_until_restored(tmp_path, monkeypatch):
    _install_noop_llm(monkeypatch)
    monkeypatch.setattr("backend.workspace.trust.is_workspace_trusted", lambda _path: True)
    conversation_dir = tmp_path / "conversations"
    monkeypatch.setattr("backend.main.CONVERSATION_DATA_DIR", conversation_dir, raising=False)
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", conversation_dir, raising=False)
    root = tmp_path / "missing-cold-workspace"
    owner = ConversationRepository(conversation_dir).create_conversation(title="Cold recovery", workspace_root=str(root))
    queue = DurableUserMessageQueue(session_id="missing-cold", root_dir=tmp_path / "user-message-queue")
    command = UserCommand("user_message", {
        "conversation_id": owner.id, "content": "cold input", "client_command_id": "cold-input",
        "user_message_id": "cold-user", "assistant_message_id": "cold-answer",
    })
    assert queue.persist_client_command(command)
    queue.close()
    calls, finished = [], threading.Event()
    monkeypatch.setattr("backend.ws.handler.WebSocketSession._run_agent", _simulate_completed_run(calls, finished))
    with TestClient(app) as client:
        with client.websocket_connect("/ws?session_id=missing-cold") as websocket:
            _receive_until_event(websocket, "llm.model.updated")
            session = _state.ws_manager.get_session("missing-cold")
            websocket.send_json({"type": "session.restore", "last_conversation_id": owner.id})
            restored = _receive_restored_with_workspace_error(websocket)
            assert restored["error"]
            websocket.send_json({"type": "session.sync"})
            _receive_until_event(websocket, "session.synced", max_attempts=100)
            assert not calls
            assert session.run_manager.durable_client_commands.has_client_command("cold-input")
            root.mkdir()
            websocket.send_json({"type": "conversation.switch", "conversation_id": owner.id})
            assert finished.wait(20)
            _receive_until_event(websocket, "done", max_attempts=100)
            assert calls == ["cold input"]


@pytest.mark.timeout(60)
def test_projectless_followups_and_restore_use_the_bound_execution_directory(tmp_path, monkeypatch):
    from backend.llm.base import LLMAdapter, StreamEvent, StreamEventType

    class CompletedLLM(LLMAdapter):
        async def stream_chat(self, messages, tools=None, metadata=None):
            yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="Completed queued input", phase="final_answer")
            yield StreamEvent(type=StreamEventType.DONE, finish_reason="stop")

        async def simple_chat(self, messages, *, max_tokens=None):
            return "Projectless"

    _install_noop_llm(monkeypatch)
    factory = lambda config, **_kwargs: CompletedLLM()
    monkeypatch.setattr("backend.main._create_session_llm", factory)
    monkeypatch.setattr("backend.llm.model_registry.create_session_llm", factory)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("backend.workspace.trust.is_workspace_trusted", lambda _path: False)
    conversation_dir = tmp_path / "conversations"
    monkeypatch.setattr("backend.main.CONVERSATION_DATA_DIR", conversation_dir, raising=False)
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", conversation_dir, raising=False)
    calls, all_finished = [], threading.Event()
    with TestClient(app) as client:
        with client.websocket_connect("/ws?session_id=projectless-recovery") as websocket:
            _receive_until_event(websocket, "llm.model.updated")
            session = _state.ws_manager.get_session("projectless-recovery")
            owner = session.conversation_repo.create_conversation(title="Projectless")
            for index in (1, 2):
                session.run_manager.enqueue_user_message(owner.id, UserCommand("user_message", {
                    "conversation_id": owner.id, "content": f"input {index}",
                    "user_message_id": f"user-{index}", "assistant_message_id": f"answer-{index}",
                }))

            run_agent = session._run_agent

            async def run(content, *args, **kwargs):
                calls.append(content)
                await run_agent(content, *args, **kwargs)
                if len(calls) == 2:
                    all_finished.set()

            monkeypatch.setattr(session, "_run_agent", run)
            websocket.send_json({"type": "session.restore", "last_conversation_id": owner.id})
            _receive_until_event(websocket, "session.restored", max_attempts=100)
            assert all_finished.wait(20)
            assert calls == ["input 1", "input 2"]
            assert session.conversation_repo.get_conversation_summary(owner.id).execution_cwd
            websocket.send_json({"type": "session.restore", "last_conversation_id": owner.id})
            restored = _receive_until_event(websocket, "session.restored", max_attempts=100)
            assert not restored["error"]


@pytest.mark.timeout(60)
def test_restore_rechecks_trust_for_an_already_mounted_workspace(tmp_path, monkeypatch):
    _install_noop_llm(monkeypatch)
    trusted = {"value": True}
    monkeypatch.setattr("backend.workspace.trust.is_workspace_trusted", lambda _path: trusted["value"])
    conversation_dir = tmp_path / "conversations"
    monkeypatch.setattr("backend.main.CONVERSATION_DATA_DIR", conversation_dir, raising=False)
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", conversation_dir, raising=False)
    root = tmp_path / "mounted-workspace"
    root.mkdir()
    calls, finished = [], threading.Event()
    with TestClient(app) as client:
        with client.websocket_connect("/ws?session_id=mounted-trust-restore") as websocket:
            _receive_until_event(websocket, "llm.model.updated")
            session = _state.ws_manager.get_session("mounted-trust-restore")
            owner = session.conversation_repo.create_conversation(title="Mounted workspace", workspace_root=str(root))
            websocket.send_json({"type": "session.restore", "last_conversation_id": owner.id})
            assert not _receive_until_event(websocket, "session.restored", max_attempts=100)["error"]

            async def wait_for_workspace():
                task = session.session_lifecycle.workspace_context_task
                if task is not None:
                    await task

            client.portal.call(wait_for_workspace)
            monkeypatch.setattr(session, "_run_agent", _simulate_completed_run(calls, finished).__get__(session))
            session.run_manager.enqueue_user_message(owner.id, UserCommand("user_message", {
                "conversation_id": owner.id, "content": "wait for workspace trust",
                "user_message_id": "trust-user", "assistant_message_id": "trust-answer",
            }))
            trusted["value"] = False
            websocket.send_json({"type": "session.restore", "last_conversation_id": owner.id})
            assert _receive_restored_with_workspace_error(websocket)["error"]
            assert not calls
            assert [item.data["content"] for item in session.run_manager.queued_user_messages(owner.id)] == ["wait for workspace trust"]
            trusted["value"] = True
            websocket.send_json({"type": "conversation.switch", "conversation_id": owner.id})
            assert finished.wait(20)
            assert calls == ["wait for workspace trust"]


@pytest.mark.asyncio
@pytest.mark.parametrize("held_stage", ["restore", "activation"])
async def test_newer_selection_supersedes_slow_restore(tmp_path, monkeypatch, held_stage):
    from backend.ws.session_restore import SessionRestoreManager

    manager = WebSocketManager()
    connection = _connection(tmp_path, session_id="restore-navigation")
    session, _ = await manager.connect(**connection)
    monkeypatch.setattr(session, "refresh_llm_selection", lambda **_: None)
    monkeypatch.setattr(session.session_lifecycle, "schedule_runtime_capabilities", lambda **_: None)
    old = session.conversation_repo.create_conversation(title="Old")
    latest = session.conversation_repo.create_conversation(title="Latest")
    entered, release = asyncio.Event(), asyncio.Event()
    restore = SessionRestoreManager.restore_session

    async def held_restore(self, *args, **kwargs):
        result = await restore(self, *args, **kwargs)
        entered.set()
        await release.wait()
        return result

    if held_stage == "restore":
        monkeypatch.setattr(SessionRestoreManager, "restore_session", held_restore)
    else:
        switch_workspace = session.switch_workspace_for_conversation

        async def held_activation(conversation, **kwargs):
            if conversation.id == old.id:
                entered.set()
                await release.wait()
            return await switch_workspace(conversation, **kwargs)

        monkeypatch.setattr(session, "switch_workspace_for_conversation", held_activation)
    first = asyncio.create_task(session.command_dispatcher._dispatch_client_command(UserCommand("session.restore", {
        "last_conversation_id": old.id, "client_command_id": "old-restore",
    }), session.connection_generation))
    second = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        second = asyncio.create_task(session.command_dispatcher._dispatch_client_command(UserCommand("conversation.switch", {
            "conversation_id": latest.id, "client_command_id": "new-selection",
        }), session.connection_generation))
        await asyncio.sleep(0)
        release.set()
        assert all(await asyncio.gather(first, second))
        await session.conversation_runtime.wait_for_hydration(latest.id)
        await session.event_outbox.drain_delivery()
        assert session.active_conversation_id == latest.id
        pages = [event for event in connection["websocket"].sent if event.get("type") == "conversation.switched"]
        assert pages and all(event.get("client_command_id") == "new-selection" for event in pages)
        assert any(event.get("type") == "session.restored" for event in connection["websocket"].sent)
    finally:
        release.set()
        await asyncio.gather(first, *([second] if second is not None else []), return_exceptions=True)
        await _release_session(session)


@pytest.mark.asyncio
@pytest.mark.parametrize("follower_failed", [False, True])
async def test_reopening_in_another_window_resumes_the_original_execution_owner(tmp_path, monkeypatch, follower_failed):
    monkeypatch.setattr("backend.workspace.trust.is_workspace_trusted", lambda _path: True)
    manager = WebSocketManager()
    first, _ = await manager.connect(**_connection(tmp_path, session_id="restore-owner"))
    second, _ = await manager.connect(**_connection(tmp_path, session_id="restore-follower"))
    for session in (first, second):
        monkeypatch.setattr(session, "refresh_llm_selection", lambda **_: None)
        monkeypatch.setattr(session.session_lifecycle, "schedule_runtime_capabilities", lambda **_: None)
    root = tmp_path / "two-window-workspace"
    owner = first.conversation_repo.create_conversation(title="Two-window recovery", workspace_root=str(root))
    manager.load_conversation_execution(owner.id, first)
    first.run_manager.enqueue_user_message(owner.id, UserCommand("user_message", {
        "conversation_id": owner.id, "content": "owned queued input",
        "user_message_id": "owner-user", "assistant_message_id": "owner-answer",
    }))
    calls, finished = [], threading.Event()
    # The direct two-window fixture has no application bootstrap; the agent
    # itself is simulated, so use the fixture's real empty registry snapshot.
    monkeypatch.setattr(first, "_conversation_tool_registry", lambda *_args, **_kwargs: first.tool_registry)
    monkeypatch.setattr(first, "_run_agent", _simulate_completed_run(calls, finished).__get__(first))
    try:
        for session in (first, second) if follower_failed else (first,):
            await session.command_dispatcher._dispatch_client_command(UserCommand("session.restore", {
                "last_conversation_id": owner.id,
            }), session.connection_generation)
            assert session.session_lifecycle.workspace_activation_failed(owner.id)
        assert not calls
        root.mkdir()
        await second.command_dispatcher._dispatch_client_command(UserCommand("conversation.switch", {
            "conversation_id": owner.id,
        }), second.connection_generation)
        assert await asyncio.to_thread(finished.wait, 3)
        assert calls == ["owned queued input"]
        assert manager.execution_session_for_conversation(owner.id) is first
    finally:
        await manager.shutdown(reason="restore-workspace-test")
        manager.reset_for_tests()


@pytest.mark.timeout(60)
def test_two_websockets_reopen_the_existing_execution_owners_queue(tmp_path, monkeypatch):
    _install_noop_llm(monkeypatch)
    monkeypatch.setattr("backend.workspace.trust.is_workspace_trusted", lambda _path: True)
    conversation_dir = tmp_path / "conversations"
    monkeypatch.setattr("backend.main.CONVERSATION_DATA_DIR", conversation_dir, raising=False)
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", conversation_dir, raising=False)
    root = tmp_path / "two-websocket-workspace"
    calls, finished = [], threading.Event()
    with TestClient(app) as client:
        with client.websocket_connect("/ws?session_id=restore-origin") as first_socket:
            _receive_until_event(first_socket, "llm.model.updated")
            with client.websocket_connect("/ws?session_id=restore-observer") as second_socket:
                _receive_until_event(second_socket, "llm.model.updated")
                first = _state.ws_manager.get_session("restore-origin")
                second = _state.ws_manager.get_session("restore-observer")
                owner = first.conversation_repo.create_conversation(title="Two sockets", workspace_root=str(root))
                client.portal.call(_state.ws_manager.load_conversation_execution, owner.id, first)
                first.run_manager.enqueue_user_message(owner.id, UserCommand("user_message", {
                    "conversation_id": owner.id, "content": "the original window's input",
                    "user_message_id": "two-window-user", "assistant_message_id": "two-window-answer",
                }))
                monkeypatch.setattr(first, "_run_agent", _simulate_completed_run(calls, finished).__get__(first))
                first_socket.send_json({"type": "session.restore", "last_conversation_id": owner.id})
                assert _receive_restored_with_workspace_error(first_socket)["error"]
                root.mkdir()
                second_socket.send_json({"type": "conversation.switch", "conversation_id": owner.id})
                assert _receive_until_event(second_socket, "conversation.switched", max_attempts=100)["conversation_id"] == owner.id
                assert finished.wait(20)
                done = _receive_until_event(first_socket, "done", max_attempts=100)
                assert done["message_id"] == "two-window-answer"
                assert calls == ["the original window's input"]
                assert _state.ws_manager.execution_session_for_conversation(owner.id) is first
                assert second.active_conversation_id == owner.id
