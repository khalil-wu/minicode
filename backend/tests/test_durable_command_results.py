from __future__ import annotations

import asyncio
from functools import partial
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from backend.agent.message import UserCommand
from backend.api import _state
from backend.conversations.repository import ConversationRepository
from backend.main import app
from backend.tests.test_conversation_activation_contract import _session
from backend.ws.command_dispatcher import SessionCommandDispatcher
from backend.ws.durable_user_queue import DurableUserMessageQueue, delete_client_results_for_conversation
from backend.ws.event_outbox import EventOutbox, capture_durable_results
from tests.test_conversations import _install_noop_llm, _receive_until_event


@pytest.mark.timeout(30)
def test_disconnected_unowned_command_replays_real_failure_over_websocket(tmp_path, monkeypatch):
    _install_noop_llm(monkeypatch)
    monkeypatch.setattr("backend.main.CONVERSATION_DATA_DIR", tmp_path, raising=False)
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", tmp_path, raising=False)
    started, release = threading.Event(), threading.Event()
    original = SessionCommandDispatcher._handle_command_inner

    async def gate(self, command):
        if command.type == "unsupported.durable.test":
            started.set()
            await asyncio.to_thread(release.wait, 10)
        return await original(self, command)

    monkeypatch.setattr(SessionCommandDispatcher, "_handle_command_inner", gate)
    with TestClient(app) as client:
        with client.websocket_connect("/ws?session_id=durable-result-test") as websocket:
            _receive_until_event(websocket, "llm.model.updated")
            websocket.send_json({"type": "unsupported.durable.test", "client_command_id": "undelivered-command"})
            ack = _receive_until_event(websocket, "client.command.ack")
            assert ack["client_command_id"] == "undelivered-command" and started.wait(5)
            session = _state.ws_manager.get_session("durable-result-test")
        release.set()
        deadline = time.monotonic() + 10
        while not session.run_manager.durable_client_commands.is_client_command_completed("undelivered-command") and time.monotonic() < deadline:
            time.sleep(.05)
        queue = session.run_manager.durable_client_commands
        assert queue.is_client_command_completed("undelivered-command")
        assert len(queue.pending_client_results()[0]) == 1
        with client.websocket_connect("/ws?session_id=durable-result-test") as websocket:
            _receive_until_event(websocket, "llm.model.updated")
            websocket.send_json({"type": "session.restore"})
            result = None
            restored = False
            while result is None or not restored:
                payload = websocket.receive_json()
                if payload.get("type") == "session.restored":
                    restored = True
                elif payload.get("type") == "command.result" and payload.get("client_command_id") == "undelivered-command":
                    result = payload
            assert result["client_command_id"] == "undelivered-command"
            assert result["command"] == "unsupported.durable.test" and result["level"] == "error"
            assert "Unsupported command" in result["message"]
            deadline = time.monotonic() + 5
            while queue.pending_client_results()[0] and time.monotonic() < deadline:
                time.sleep(.05)
            assert not queue.pending_client_results()[0]


@pytest.mark.timeout(45)
def test_unowned_failure_finishing_after_restore_reaches_the_current_connection(tmp_path, monkeypatch):
    _install_noop_llm(monkeypatch)
    monkeypatch.setattr("backend.main.CONVERSATION_DATA_DIR", tmp_path, raising=False)
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", tmp_path, raising=False)
    started, release, restored, delivered = (threading.Event() for _ in range(4))
    original = SessionCommandDispatcher._handle_command_inner

    async def gate(self, command):
        if command.type == "unsupported.late.test":
            started.set()
            await asyncio.to_thread(release.wait, 15)
        return await original(self, command)

    monkeypatch.setattr(SessionCommandDispatcher, "_handle_command_inner", gate)
    with TestClient(app) as client:
        try:
            with client.websocket_connect("/ws?session_id=late-failure-test") as websocket:
                _receive_until_event(websocket, "llm.model.updated")
                session = _state.ws_manager.get_session("late-failure-test")
                websocket.send_json({"type": "unsupported.late.test", "client_command_id": "late-failure"})
                assert _receive_until_event(websocket, "client.command.ack")["client_command_id"] == "late-failure"
                assert started.wait(5)
            replay = session.command_dispatcher._replay_pending_client_commands

            async def observe_restore(generation, *, replay_results=True):
                await replay(generation, replay_results=replay_results)
                if replay_results:
                    restored.set()

            monkeypatch.setattr(session.command_dispatcher, "_replay_pending_client_commands", observe_restore)
            with client.websocket_connect("/ws?session_id=late-failure-test") as websocket:
                _receive_until_event(websocket, "llm.model.updated")
                send_json = session.ws.send_json

                async def observe_delivery(payload):
                    await send_json(payload)
                    if payload.get("type") == "command.result" and payload.get("client_command_id") == "late-failure":
                        delivered.set()

                monkeypatch.setattr(session.ws, "send_json", observe_delivery)
                websocket.send_json({"type": "session.restore"})
                assert restored.wait(10)
                release.set()
                assert delivered.wait(10)
                result = None
                while result is None:
                    payload = websocket.receive_json()
                    if payload.get("type") == "command.result" and payload.get("client_command_id") == "late-failure":
                        result = payload
                assert result["command"] == "unsupported.late.test" and result["level"] == "error"
                assert "Unsupported command" in result["message"] and result["event_id"]
        finally:
            release.set()


@pytest.mark.timeout(30)
def test_owned_result_replay_precedes_unsequenced_receipt_on_real_reconnect(tmp_path, monkeypatch):
    _install_noop_llm(monkeypatch)
    monkeypatch.setattr("backend.main.CONVERSATION_DATA_DIR", tmp_path, raising=False)
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", tmp_path, raising=False)
    started, release = threading.Event(), threading.Event()
    original = SessionCommandDispatcher._handle_command_inner

    async def gate(self, command):
        if command.type == "unsupported.owned.test":
            started.set()
            await asyncio.to_thread(release.wait, 10)
        return await original(self, command)

    monkeypatch.setattr(SessionCommandDispatcher, "_handle_command_inner", gate)
    with TestClient(app) as client:
        with client.websocket_connect("/ws?session_id=owned-result-test") as websocket:
            _receive_until_event(websocket, "llm.model.updated")
            session = _state.ws_manager.get_session("owned-result-test")
            owner = session.conversation_repo.create_conversation(title="Owned result")
            session.active_conversation_id = owner.id
            client.portal.call(partial(session.send_payload, {"type": "command.result", "command": "anchor",
                "level": "info", "message": "anchor", "conversation_id": owner.id}, log_context="test anchor"))
            anchor = _receive_until_event(websocket, "command.result")
            assert anchor["seq"] > 0
            websocket.send_json({"type": "unsupported.owned.test", "conversation_id": owner.id,
                                 "client_command_id": "owned-command"})
            _receive_until_event(websocket, "client.command.ack")
            assert started.wait(5)
        release.set()
        queue = session.run_manager.durable_client_commands
        deadline = time.monotonic() + 10
        while not queue.is_client_command_completed("owned-command") and time.monotonic() < deadline:
            time.sleep(.05)
        assert queue.is_client_command_completed("owned-command")
        with client.websocket_connect("/ws?session_id=owned-result-test") as websocket:
            _receive_until_event(websocket, "llm.model.updated")
            websocket.send_json({"type": "session.restore", "last_conversation_id": owner.id, "last_seq": anchor["seq"]})
            replay, receipt, seen = None, None, []
            while replay is None or receipt is None:
                payload = websocket.receive_json()
                seen.append(payload["type"])
                if payload["type"] == "session.replay":
                    replay = next((item for item in payload["events"] if item.get("client_command_id") == "owned-command"), None)
                if payload["type"] == "command.result" and payload.get("client_command_id") == "owned-command":
                    receipt = payload
            assert seen.index("session.replay") < seen.index("command.result")
            assert receipt["event_id"] == replay["event_id"] and "seq" not in receipt


@pytest.mark.asyncio
async def test_process_exit_after_result_archive_reconciles_without_reexecution(tmp_path):
    program = r'''
import os,sys
from pathlib import Path
from backend.agent.message import UserCommand
from backend.ws.durable_user_queue import DurableUserMessageQueue
root=Path(sys.argv[1])
queue=DurableUserMessageQueue(session_id='crashed-result',root_dir=root)
command=UserCommand('unsupported.durable.test',{'client_command_id':'crashed-command'})
assert queue.persist_client_command(command)
assert queue.claim_client_command('crashed-command') is not None
queue.archive_client_results('crashed-command',[{'type':'command.result','command':'unsupported.durable.test',
    'client_command_id':'crashed-command','level':'error','message':'actual failure','event_id':'original-event'}])
os._exit(91)
'''
    result = await asyncio.to_thread(subprocess.run, [sys.executable, "-c", program, str(tmp_path / "queue")],
                                     cwd=Path(__file__).resolve().parents[2], capture_output=True, timeout=30)
    assert result.returncode == 91, result.stderr
    repository = ConversationRepository(tmp_path / "conversations")
    session = _session(repository, "")
    session.session_id = "crashed-result"
    session.connection_generation = 1
    queue = DurableUserMessageQueue(session_id=session.session_id, root_dir=tmp_path / "queue")
    session.run_manager = SimpleNamespace(durable_client_commands=queue, durable_queue=SimpleNamespace(load_error=None))

    class Socket:
        def __init__(self):
            self.messages = []
        async def send_json(self, payload):
            self.messages.append(payload)

    socket = Socket()
    session.event_outbox = EventOutbox(session_id=session.session_id, websocket=socket, replay_root=tmp_path / "replay",
        replay_limit=20, cleanup_tasks=set(), has_active_run=lambda: False,
        requires_conversation_owner=lambda *_: False, workspace_scoped_event_types=())
    dispatcher = SessionCommandDispatcher(session, root_dir=tmp_path / "dedup")
    try:
        await dispatcher._replay_pending_client_commands(1)
        assert queue.is_client_command_completed("crashed-command")
        assert not queue.has_client_command("crashed-command")
        assert not queue.pending_client_results()[0]
        assert [message["event_id"] for message in socket.messages] == ["original-event"]
        assert socket.messages[0]["message"] == "actual failure"
        assert "seq" not in socket.messages[0]
        assert not dispatcher.command_tasks
    finally:
        queue.close()


@pytest.mark.asyncio
async def test_failed_result_archive_retries_commit_without_reexecuting(tmp_path, monkeypatch):
    repository = ConversationRepository(tmp_path / "conversations")
    session = _session(repository, "")
    session.session_id, session.connection_generation = "result-retry", 1
    queue = DurableUserMessageQueue(session_id=session.session_id, root_dir=tmp_path / "queue")
    session.run_manager = SimpleNamespace(durable_client_commands=queue, durable_queue=None)
    session.event_outbox = EventOutbox(session_id=session.session_id, websocket=None, replay_root=tmp_path / "replay",
        replay_limit=20, cleanup_tasks=set(), has_active_run=lambda: False,
        requires_conversation_owner=lambda *_: False, workspace_scoped_event_types=())
    session.event_outbox.connected = False
    dispatcher = SessionCommandDispatcher(session, root_dir=tmp_path / "dedup")
    dispatcher.cancelled_user_message = AsyncMock(return_value=None)
    calls = []

    async def dispatch(command, generation):
        calls.append(command.type)
        with session.event_outbox.bind_client_command(command.data["client_command_id"], command.type):
            await session.event_outbox.send_payload({"type": "command.result", "command": command.type,
                "level": "error", "message": "actual refusal"}, log_context="test", wait_for_delivery=False)
        return True

    dispatcher._dispatch_client_command = dispatch
    command = UserCommand("fixture", {"client_command_id": "retry-result"})
    assert queue.persist_client_command(command)
    with monkeypatch.context() as fault:
        fault.setattr(queue, "archive_client_results", lambda *_args: (_ for _ in ()).throw(OSError("result store unavailable")))
        with pytest.raises(OSError, match="result store unavailable"):
            await dispatcher._run_durable_client_command("retry-result", 1)
    assert queue.has_client_command("retry-result") and calls == ["fixture"]
    await dispatcher._run_durable_client_command("retry-result", 1)
    assert calls == ["fixture"] and queue.is_client_command_completed("retry-result")
    assert queue.pending_client_results()[0][0]["events"][0]["message"] == "actual refusal"
    queue.close()


@pytest.mark.asyncio
async def test_stale_restore_cannot_suppress_a_later_result_on_the_current_connection(tmp_path):
    repository = ConversationRepository(tmp_path / "conversations")
    session = _session(repository, "")
    session.session_id, session.connection_generation = "stale-restore", 2
    queue = DurableUserMessageQueue(session_id=session.session_id, root_dir=tmp_path / "queue")
    session.run_manager = SimpleNamespace(durable_client_commands=queue, durable_queue=SimpleNamespace(load_error=None))

    class Socket:
        def __init__(self): self.messages = []
        async def send_json(self, payload): self.messages.append(payload)

    socket = Socket()
    outbox = EventOutbox(session_id=session.session_id, websocket=socket, replay_root=tmp_path / "replay",
        replay_limit=20, cleanup_tasks=set(), has_active_run=lambda: False,
        requires_conversation_owner=lambda *_: False, workspace_scoped_event_types=())
    outbox.connection_generation = 2
    session.event_outbox = outbox
    dispatcher = SessionCommandDispatcher(session, root_dir=tmp_path / "dedup")
    dispatcher.cancelled_user_message = AsyncMock(return_value=None)
    dispatcher._result_replay_generation = 2

    async def dispatch(command, _generation):
        with outbox.bind_client_command(command.data["client_command_id"], command.type):
            await outbox.send_payload({"type": "command.result", "command": command.type,
                "level": "error", "message": "old execution failure"}, connection_generation=1, log_context="test")
        return True

    dispatcher._dispatch_client_command = dispatch
    try:
        await dispatcher._replay_pending_client_commands(1)
        command = UserCommand("fixture", {"client_command_id": "late-after-stale-restore"})
        assert queue.persist_client_command(command)
        await dispatcher._run_durable_client_command("late-after-stale-restore", 1)
        await asyncio.gather(*tuple(dispatcher.command_tasks))
        assert len(socket.messages) == 1
        assert socket.messages[0]["message"] == "old execution failure"
        assert not queue.pending_client_results()[0]
    finally:
        queue.close()


def test_damaged_result_receipt_keeps_its_identity_and_other_results(tmp_path):
    queue = DurableUserMessageQueue(session_id="damaged-result", root_dir=tmp_path)
    try:
        for command_id in ("bad-result", "healthy-result"):
            command = UserCommand("fixture", {"client_command_id": command_id})
            assert queue.persist_client_command(command)
            assert queue.claim_client_command(command_id) is not None
            queue.archive_client_results(command_id, [{"type": "command.result", "command": "fixture",
                "client_command_id": command_id, "event_id": command_id + "-event", "level": "error", "message": "failure"}])
            assert queue.complete_client_command(command_id)
        path = queue._result_receipt_path("bad-result")
        path.write_bytes(b"{broken")
        records, errors = queue.pending_client_results()
        assert [record["client_command_id"] for record in records] == ["healthy-result"]
        assert len(errors) == 1 and errors[0]["path"] == str(path)
        assert queue.is_client_command_completed("bad-result")
        assert path.read_bytes() == b"{broken"
    finally:
        queue.close()


@pytest.mark.asyncio
async def test_result_recovery_reports_corruption_without_blocking_healthy_results(tmp_path):
    repository = ConversationRepository(tmp_path / "conversations")
    session = _session(repository, "")
    session.session_id, session.connection_generation = "result-corruption", 1
    queue = DurableUserMessageQueue(session_id=session.session_id, root_dir=tmp_path / "queue")
    session.run_manager = SimpleNamespace(durable_client_commands=queue, durable_queue=SimpleNamespace(load_error=None))
    for command_id in ("bad-result", "healthy-result"):
        assert queue.persist_client_command(UserCommand("fixture", {"client_command_id": command_id}))
        assert queue.claim_client_command(command_id) is not None
        queue.archive_client_results(command_id, [{"type": "command.result", "command": "fixture",
            "client_command_id": command_id, "event_id": command_id + "-event", "level": "error", "message": command_id}])
        assert queue.complete_client_command(command_id)
    bad = queue._result_receipt_path("bad-result")
    original = bad.read_bytes()
    bad.write_bytes(b"{broken")

    class Socket:
        def __init__(self): self.messages = []
        async def send_json(self, payload): self.messages.append(payload)

    socket = Socket()
    outbox = EventOutbox(session_id=session.session_id, websocket=socket, replay_root=tmp_path / "replay",
        replay_limit=20, cleanup_tasks=set(), has_active_run=lambda: False,
        requires_conversation_owner=lambda *_: False, workspace_scoped_event_types=())
    session.event_outbox = outbox
    session.send_payload = outbox.send_payload
    dispatcher = SessionCommandDispatcher(session, root_dir=tmp_path / "dedup")
    try:
        await dispatcher._replay_pending_client_commands(1)
        assert [item["type"] for item in socket.messages] == ["error", "command.result"]
        assert socket.messages[1]["client_command_id"] == "healthy-result"
        assert bad.read_bytes() == b"{broken"
        bad.write_bytes(original)
        await dispatcher._replay_pending_client_commands(1)
        assert socket.messages[-1]["client_command_id"] == "bad-result"
        bad.write_bytes(b"{broken")
        await dispatcher._replay_pending_client_commands(1)
        assert [item["type"] for item in socket.messages].count("error") == 2
        assert bad.read_bytes() == b"{broken"
    finally:
        queue.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("new_connection", [False, True])
async def test_late_live_delivery_is_bound_to_its_connection_generation(tmp_path, new_connection):
    repository = ConversationRepository(tmp_path / "conversations")
    session = _session(repository, "")
    session.session_id, session.connection_generation = "late-result", 1
    queue = DurableUserMessageQueue(session_id=session.session_id, root_dir=tmp_path / "queue")
    session.run_manager = SimpleNamespace(durable_client_commands=queue, durable_queue=SimpleNamespace(load_error=None))
    started, release = asyncio.Event(), asyncio.Event()

    class Socket:
        def __init__(self):
            self.messages = []
        async def send_json(self, payload):
            started.set()
            await release.wait()
            self.messages.append(payload)

    socket = Socket()
    session.event_outbox = EventOutbox(session_id=session.session_id, websocket=socket, replay_root=tmp_path / "replay",
        replay_limit=20, cleanup_tasks=set(), has_active_run=lambda: False,
        requires_conversation_owner=lambda *_: False, workspace_scoped_event_types=())
    dispatcher = SessionCommandDispatcher(session, root_dir=tmp_path / "dedup")
    dispatcher.cancelled_user_message = AsyncMock(return_value=None)

    async def dispatch(command, _generation):
        with session.event_outbox.bind_client_command(command.data["client_command_id"], command.type):
            await session.event_outbox.send_payload({"type": "command.result", "command": command.type,
                "level": "error", "message": "actual refusal"}, log_context="test", wait_for_delivery=False)
        return True

    dispatcher._dispatch_client_command = dispatch
    assert queue.persist_client_command(UserCommand("fixture", {"client_command_id": "late-command"}))
    try:
        await dispatcher._run_durable_client_command("late-command", 1)
        assert started.is_set() and queue.is_client_command_completed("late-command")
        assert queue.pending_client_results()[0]
        if new_connection:
            replacement = Socket()
            session.event_outbox.websocket = replacement
            session.event_outbox.connection_generation = 2
            session.connection_generation = 2
        release.set()
        await asyncio.gather(*list(dispatcher.command_tasks))
        if new_connection:
            assert queue.pending_client_results()[0]
            await dispatcher._replay_pending_client_commands(2)
            assert len(replacement.messages) == 1 and replacement.messages[0]["message"] == "actual refusal"
        else:
            await dispatcher._replay_pending_client_commands(1)
        assert not queue.pending_client_results()[0]
        assert len(socket.messages) == 1 and socket.messages[0]["message"] == "actual refusal"
    finally:
        release.set()
        queue.close()


@pytest.mark.asyncio
async def test_recovered_owned_result_does_not_create_an_extra_replay_cursor_entry(tmp_path):
    repository = ConversationRepository(tmp_path / "conversations")
    owner = repository.create_conversation()
    session = _session(repository, owner.id)
    session.session_id, session.connection_generation = "owned-result", 1
    queue = DurableUserMessageQueue(session_id=session.session_id, root_dir=tmp_path / "queue")
    session.run_manager = SimpleNamespace(durable_client_commands=queue, durable_queue=SimpleNamespace(load_error=None))
    outbox = EventOutbox(session_id=session.session_id, websocket=None, replay_root=tmp_path / "replay",
        replay_limit=20, cleanup_tasks=set(), has_active_run=lambda: False,
        requires_conversation_owner=lambda *_: False, workspace_scoped_event_types=())
    outbox.connected = False
    session.event_outbox = outbox
    dispatcher = SessionCommandDispatcher(session, root_dir=tmp_path / "dedup")
    dispatcher.cancelled_user_message = AsyncMock(return_value=None)

    async def dispatch(command, _generation):
        with outbox.bind_client_command(command.data["client_command_id"], command.type, owner=(owner.id, "")):
            await outbox.send_payload({"type": "command.result", "command": command.type,
                "level": "error", "message": "owned failure"}, log_context="test", wait_for_delivery=False)
        return True

    dispatcher._dispatch_client_command = dispatch
    assert queue.persist_client_command(UserCommand("fixture", {"client_command_id": "owned-command"}))
    try:
        await dispatcher._run_durable_client_command("owned-command", 1)
        assert len(outbox._events) == 1
        event_id = outbox._events[0]["event_id"]

        class Socket:
            def __init__(self): self.messages = []
            async def send_json(self, payload): self.messages.append(payload)

        socket = Socket()
        outbox.websocket, outbox.connected, outbox.connection_generation = socket, True, 2
        session.connection_generation = 2
        await dispatcher._replay_pending_client_commands(2)
        assert len(outbox._events) == 1
        assert socket.messages[0]["event_id"] == event_id and "seq" not in socket.messages[0]
        assert not queue.pending_client_results()[0]
    finally:
        if outbox.persistence_tail is not None:
            await outbox.persistence_tail
        queue.close()


def test_deleted_conversation_clears_results_across_sessions_without_losing_other_owners(tmp_path):
    queues = [DurableUserMessageQueue(session_id=name, root_dir=tmp_path) for name in ("first", "second")]
    try:
        for index, queue in enumerate(queues):
            command_id = f"command-{index}"
            assert queue.persist_client_command(UserCommand("fixture", {"client_command_id": command_id}))
            assert queue.claim_client_command(command_id) is not None
            queue.archive_client_results(command_id, [
                {"type": "command.result", "command": "fixture", "client_command_id": command_id,
                 "event_id": f"deleted-{index}", "level": "error", "message": "deleted content", "conversation_id": "deleted"},
                {"type": "command.result", "command": "fixture", "client_command_id": command_id,
                 "event_id": f"other-{index}", "level": "error", "message": "keep other", "conversation_id": "other"},
            ])
            assert queue.complete_client_command(command_id)
        bad = queues[0]._result_receipt_dir / "damaged.json"
        bad.write_bytes(b"{broken")
        with pytest.raises(RuntimeError, match="could not be inspected"):
            delete_client_results_for_conversation(tmp_path, "deleted")
        assert bad.read_bytes() == b"{broken"
        for queue in queues:
            records, _errors = queue.pending_client_results()
            assert [event["conversation_id"] for record in records for event in record["events"]] == ["other"]
    finally:
        for queue in queues:
            queue.close()


@pytest.mark.asyncio
async def test_forwarded_failure_is_captured_when_origin_window_is_gone(tmp_path):
    from backend.ws.conversation_stream import bind_response_origin
    from backend.ws.handler import WebSocketSession

    outbox = EventOutbox(session_id="owner-window", websocket=None, replay_root=tmp_path / "replay",
        replay_limit=20, cleanup_tasks=set(), has_active_run=lambda: False,
        requires_conversation_owner=lambda *_: False, workspace_scoped_event_types=())
    owner = SimpleNamespace(session_id="owner-window", event_outbox=outbox,
                            ws_manager=SimpleNamespace(get_session=lambda _session_id: None))
    with capture_durable_results("forwarded-command") as capture, bind_response_origin("missing-window", "forwarded-command"), \
            outbox.bind_client_command("forwarded-command", "subagent.cancel", owner=("conversation", "workspace")):
        sent = await WebSocketSession.send_payload(owner, {"type": "command.result", "command": "subagent.cancel",
            "client_command_id": "forwarded-command", "level": "error", "message": "actual refusal",
            "conversation_id": "conversation", "workspace_root": "workspace"}, log_context="test")
    assert not sent and len(capture.deliveries) == 1
    assert capture.deliveries[0].receipt is None
    assert capture.deliveries[0].payload["conversation_id"] == "conversation"


@pytest.mark.asyncio
@pytest.mark.parametrize("command_type,kept", [("fixture", False), ("conversation.delete", True)])
async def test_result_finishing_after_conversation_deletion_respects_its_owner(tmp_path, command_type, kept):
    repository = ConversationRepository(tmp_path / "conversations")
    owner = repository.create_conversation(title="Deleted owner")
    session = _session(repository, owner.id)
    session.session_id, session.connection_generation = "deletion-race", 1
    queue = DurableUserMessageQueue(session_id=session.session_id, root_dir=tmp_path / "queue")
    session.run_manager = SimpleNamespace(durable_client_commands=queue, durable_queue=None)
    outbox = EventOutbox(session_id=session.session_id, websocket=None, replay_root=tmp_path / "replay",
        replay_limit=20, cleanup_tasks=set(), has_active_run=lambda: False,
        requires_conversation_owner=lambda *_: False, workspace_scoped_event_types=())
    outbox.connected = False
    session.event_outbox = outbox
    dispatcher = SessionCommandDispatcher(session, root_dir=tmp_path / "dedup")
    dispatcher.cancelled_user_message = AsyncMock(return_value=None)

    async def dispatch(command, _generation):
        with outbox.bind_client_command(command.data["client_command_id"], command.type, owner=(owner.id, "")):
            await outbox.send_payload({"type": "command.result", "command": command.type, "level": "error",
                "message": "deleted content"}, log_context="test", wait_for_delivery=False)
        assert repository.delete_conversation(owner.id)
        return True

    dispatcher._dispatch_client_command = dispatch
    assert queue.persist_client_command(UserCommand(command_type, {"client_command_id": "deleted-owner-command"}))
    try:
        await dispatcher._run_durable_client_command("deleted-owner-command", 1)
        assert queue.is_client_command_completed("deleted-owner-command")
        assert bool(queue.pending_client_results()[0]) is kept
    finally:
        if outbox.persistence_tail is not None:
            await outbox.persistence_tail
        queue.close()
