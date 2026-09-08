from __future__ import annotations

import asyncio
import itertools
import json
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest
from fastapi import WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from backend import main
from backend.artifact.store import ArtifactStore
from backend.config import AppConfig, LLMSettings
from backend.permissions.checker import PermissionChecker
from backend.tools.registry import ToolRegistry
from backend.ws import handler
from backend.ws.event_log import WebSocketReplayEventStore
from backend.ws.event_outbox import EventOutbox
from backend.ws.manager import WebSocketManager


class RecordingSocket:
    def __init__(self, session_id: str):
        self.query_params = {"session_id": session_id}
        self.headers = {}
        self.application_state = WebSocketState.CONNECTING
        self.accept_calls = 0
        self.sent = []
        self.closed = []

    async def accept(self, **kwargs):
        self.accept_calls += 1
        self.application_state = WebSocketState.CONNECTED

    async def send_json(self, payload):
        self.sent.append(payload)

    async def close(self, **kwargs):
        self.closed.append(kwargs)
        self.application_state = WebSocketState.DISCONNECTED


class RecordingLLM:
    def __init__(self):
        self.close_calls = 0

    async def aclose(self):
        self.close_calls += 1


class RecordingArtifacts(ArtifactStore):
    def __init__(self, storage_dir: Path):
        super().__init__(storage_dir=storage_dir)
        self.flush_calls = 0
        self.shutdown_calls = 0
        self.clear_calls = 0

    async def flush(self):
        self.flush_calls += 1
        await super().flush()

    def shutdown(self):
        self.shutdown_calls += 1
        super().shutdown()

    def clear(self):
        self.clear_calls += 1
        super().clear()


def _connection(tmp_path, session_id="cold-session"):
    config = AppConfig(llm=LLMSettings(api_key=""))
    return {
        "websocket": RecordingSocket(session_id),
        "llm": RecordingLLM(),
        "artifact_store": RecordingArtifacts(tmp_path / "artifacts"),
        "tool_registry": ToolRegistry(),
        "permission_checker": PermissionChecker(config.permissions, workspace_root=None),
        "config": config,
        "mcp_manager": None,
    }


def _replay_path(session_id="cold-session"):
    root = Path(handler.CONVERSATION_DATA_DIR).parent / "ws-event-log"
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{session_id}.jsonl"


def _assert_released(connection):
    assert connection["llm"].close_calls == 1
    artifacts = connection["artifact_store"]
    assert (artifacts.flush_calls, artifacts.shutdown_calls, artifacts.clear_calls) == (1, 1, 1)


async def _release_session(session):
    await session.run_manager.shutdown_notification_wakes()
    session.run_manager.close_durable_queue()
    await session.event_outbox.drain_persistence()
    await session.llm.aclose()
    session.artifact_store.shutdown()
    session.artifact_store.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed_tail", [False, True])
async def test_cold_connect_loads_once_off_loop_and_preserves_replay_status(
    tmp_path, monkeypatch, malformed_tail,
):
    payload = {
        "type": "agent_message.delta", "conversation_id": "conversation",
        "item_id": "message", "delta": "保留内容", "seq": 42,
    }
    content = json.dumps(payload, ensure_ascii=False) + "\n"
    if malformed_tail:
        content += "{broken\n"
    path = _replay_path()
    path.write_text(content, encoding="utf-8")
    entered = threading.Event()
    release = threading.Event()
    threads = []
    load = WebSocketReplayEventStore.load

    def held_load(store, **kwargs):
        threads.append(threading.get_ident())
        entered.set()
        assert release.wait(3)
        return load(store, **kwargs)

    monkeypatch.setattr(WebSocketReplayEventStore, "load", held_load)
    manager = WebSocketManager()
    connection = _connection(tmp_path)
    connecting = asyncio.create_task(manager.connect(**connection))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        assert not connecting.done()
        assert manager.iter_sessions() == ()
    finally:
        release.set()
    session, generation = await connecting
    try:
        assert generation == 1
        assert len(threads) == 1
        assert threads[0] != threading.get_ident()
        assert session.run_manager._notification_loop is asyncio.get_running_loop()
        assert session.event_outbox.current_replay_seq == 42
        assert session.event_outbox.replay_log_degraded is malformed_tail
        assert session.event_outbox._events == [payload]
        assert path.read_text(encoding="utf-8") == content
        assert connection["llm"].close_calls == 0
        assert connection["artifact_store"].shutdown_calls == 0
    finally:
        await _release_session(session)


@pytest.mark.asyncio
@pytest.mark.parametrize("first_to_finish", [0, 1])
async def test_simultaneous_cold_connections_only_adopt_latest_socket(
    tmp_path, monkeypatch, first_to_finish,
):
    load = EventOutbox.load_replay_state
    indexes = itertools.count()
    entered = [threading.Event(), threading.Event()]
    release = [threading.Event(), threading.Event()]

    def held_load(**kwargs):
        index = next(indexes)
        entered[index].set()
        assert release[index].wait(3)
        return load(**kwargs)

    monkeypatch.setattr(EventOutbox, "load_replay_state", staticmethod(held_load))
    manager = WebSocketManager()
    first = _connection(tmp_path / "first")
    second = _connection(tmp_path / "second")
    first_connect = asyncio.create_task(manager.connect(**first))
    assert await asyncio.to_thread(entered[0].wait, 2)
    second_connect = asyncio.create_task(manager.connect(**second))
    try:
        assert await asyncio.to_thread(entered[1].wait, 2)
        release[first_to_finish].set()
        if first_to_finish == 0:
            with pytest.raises(WebSocketDisconnect) as rejected:
                await first_connect
            assert rejected.value.code == 1012
            assert manager.iter_sessions() == ()
        else:
            await second_connect
            release[0].set()
            with pytest.raises(WebSocketDisconnect) as rejected:
                await first_connect
            assert rejected.value.code == 1012
        release[1].set()
        second_session, second_generation = await second_connect
        assert second_generation == 1
        assert manager.iter_sessions() == (second_session,)
        assert second_session.ws is second["websocket"]
        assert second_session.llm is second["llm"]
        assert second_session.artifact_store is second["artifact_store"]
        assert first["websocket"].closed == [
            {"code": 1012, "reason": "replaced by newer connection"},
        ]
        assert second["websocket"].closed == []
        assert second["llm"].close_calls == 0
        assert not manager._disconnect_tasks
        assert not manager._connecting_websockets
        _assert_released(first)
    finally:
        for gate in release:
            gate.set()
        await asyncio.gather(first_connect, second_connect, return_exceptions=True)
        for session in manager.iter_sessions():
            await _release_session(session)


@pytest.mark.asyncio
async def test_cancelling_cold_read_releases_unadopted_resources(tmp_path, monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    load = EventOutbox.load_replay_state

    def held_load(**kwargs):
        entered.set()
        try:
            assert release.wait(3)
            return load(**kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(EventOutbox, "load_replay_state", staticmethod(held_load))
    manager = WebSocketManager()
    connection = _connection(tmp_path)
    connecting = asyncio.create_task(manager.connect(**connection))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        connecting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await connecting
        _assert_released(connection)
        assert manager.iter_sessions() == ()
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 2)
    assert manager.iter_sessions() == ()


@pytest.mark.asyncio
async def test_corrupt_cold_replay_releases_resources_without_erasing_log(tmp_path):
    path = _replay_path()
    path.write_bytes(b"\xff\n")
    manager = WebSocketManager()
    connection = _connection(tmp_path)
    with pytest.raises(UnicodeDecodeError):
        await manager.connect(**connection)
    _assert_released(connection)
    assert manager.iter_sessions() == ()
    assert path.read_bytes() == b"\xff\n"


@pytest.mark.asyncio
async def test_warm_reconnect_does_not_reload_or_close_adopted_resources(tmp_path, monkeypatch):
    manager = WebSocketManager()
    connection = _connection(tmp_path)
    session, _generation = await manager.connect(**connection)

    def unexpected_load(**kwargs):
        pytest.fail("A warm reconnect must not read replay state")

    monkeypatch.setattr(EventOutbox, "load_replay_state", staticmethod(unexpected_load))
    try:
        connection["websocket"] = RecordingSocket("cold-session")
        restored, generation = await manager.connect(**connection)
        assert restored is session
        assert generation == 2
        assert connection["llm"].close_calls == 0
        assert connection["artifact_store"].shutdown_calls == 0
    finally:
        await _release_session(session)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["invalid_id", "accept"])
async def test_rejected_connect_releases_resources(tmp_path, failure):
    manager = WebSocketManager()
    connection = _connection(tmp_path, session_id="../invalid")
    expected_error = WebSocketDisconnect
    if failure == "accept":
        async def failed_accept(**kwargs):
            raise OSError("transport closed")

        connection["websocket"].accept = failed_accept
        expected_error = OSError
    with pytest.raises(expected_error):
        await manager.connect(**connection)
    _assert_released(connection)
    assert manager.iter_sessions() == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(("failure", "drop_at"), [
    ("artifact", None), ("registry", None), ("permissions", None), ("replay", None),
    ("replay", "send"), ("replay", "close"),
])
async def test_endpoint_reports_initialization_failure_and_releases_resources(
    tmp_path, monkeypatch, failure, drop_at,
):
    connection = _connection(tmp_path)
    artifacts = connection["artifact_store"]
    llm = connection["llm"]
    manager = WebSocketManager()
    socket = connection["websocket"]
    endpoint_socket = socket
    if drop_at is not None:
        async def receive():
            return {"type": "websocket.connect"}

        async def send(message):
            if message["type"] == "websocket.accept":
                await socket.accept()
            elif message["type"] == "websocket.send":
                if drop_at == "send":
                    raise OSError("client disconnected while sending error")
                await socket.send_json(json.loads(message["text"]))
            else:
                await socket.close(code=message["code"])
                raise OSError("client disconnected while closing")

        endpoint_socket = WebSocket(
            {"type": "websocket", "headers": [], "query_string": b"session_id=cold-session"},
            receive=receive, send=send,
        )
    path = _replay_path()
    path.write_bytes(b"\xff\n")

    def create_artifacts():
        if failure == "artifact":
            path = tmp_path / "artifact-file"
            path.write_text("occupied", encoding="utf-8")
            return ArtifactStore(storage_dir=path)
        return artifacts

    def create_registry(*args, **kwargs):
        if failure == "registry":
            raise OSError("tool registry unavailable")
        return connection["tool_registry"]

    def create_permissions(**kwargs):
        if failure == "permissions":
            raise OSError("permission config unavailable")
        return connection["permission_checker"]

    bootstrap = SimpleNamespace(
        create_llm=lambda **kwargs: llm,
        create_tool_registry=create_registry,
        create_permission_checker=create_permissions,
        skill_manager=None, skill_executor=None, memory_manager=None, mcp_manager=None,
    )
    monkeypatch.setattr(main, "_is_websocket_authorized", lambda socket: True)
    monkeypatch.setattr(main, "_websocket_origin_allowed", lambda socket: True)
    monkeypatch.setattr(main, "load_config", lambda **kwargs: connection["config"])
    monkeypatch.setattr(main, "ArtifactStore", create_artifacts)
    monkeypatch.setattr(main._state, "bootstrap", bootstrap)
    monkeypatch.setattr(main._state, "ws_manager", manager)
    await main.websocket_endpoint(endpoint_socket)
    assert manager.iter_sessions() == ()
    assert socket.accept_calls == 1
    if drop_at == "send":
        assert socket.closed == []
        assert socket.sent == []
    else:
        assert socket.closed == [{"code": 1011}]
        assert len(socket.sent) == 1
        assert socket.sent[0]["type"] == "error"
        assert socket.sent[0]["error_code"] == "connection.session_initialization_failed"
        assert socket.sent[0]["recoverable"] is False
        assert socket.sent[0]["message"].startswith("Session initialization failed:")
    assert llm.close_calls == 1
    assert path.read_bytes() == b"\xff\n"
    if failure != "artifact":
        _assert_released(connection)
    else:
        assert artifacts.shutdown_calls == 0
        artifacts.shutdown()
