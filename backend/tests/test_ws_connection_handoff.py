from __future__ import annotations

import asyncio
import json
import threading

import pytest
from fastapi import WebSocket, WebSocketDisconnect

from backend.tests.test_ws_cold_connection import (
    _assert_released,
    _connection,
    _release_session,
)
from backend.ws.event_outbox import EventOutbox
from backend.ws.manager import WebSocketManager


@pytest.mark.asyncio
@pytest.mark.parametrize("newer_takes_over", [False, True])
async def test_cancelled_reconnect_only_disconnects_its_adopted_socket(
    tmp_path, newer_takes_over,
):
    manager = WebSocketManager()
    first = _connection(tmp_path / "first")
    incoming = _connection(tmp_path / "incoming")
    newest = _connection(tmp_path / "newest")
    session, _generation = await manager.connect(**first)
    close_entered = asyncio.Event()

    async def held_close(**kwargs):
        close_entered.set()
        await asyncio.Event().wait()

    first["websocket"].close = held_close
    connecting = asyncio.create_task(manager.connect(**incoming))
    try:
        await asyncio.wait_for(close_entered.wait(), 2)
        if newer_takes_over:
            restored, generation = await manager.connect(**newest)
            assert restored is session
            assert generation == 3
        connecting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await connecting
        assert session.is_connected is newer_takes_over
        assert ("cold-session" in manager._disconnect_tasks) is not newer_takes_over
        assert session.ws is (newest if newer_takes_over else incoming)["websocket"]
        assert first["llm"].close_calls == 0
        assert first["artifact_store"].shutdown_calls == 0
        _assert_released(incoming)
        assert not manager._connecting_websockets
        if not newer_takes_over:
            restored, generation = await manager.connect(**newest)
            assert restored is session
            assert generation == 3
        assert session.is_connected
        assert not manager._disconnect_tasks
        _assert_released(newest)
    finally:
        connecting.cancel()
        await asyncio.gather(connecting, return_exceptions=True)
        for task in manager._disconnect_tasks.values():
            task.cancel()
        await asyncio.gather(*manager._disconnect_tasks.values(), return_exceptions=True)
        await _release_session(session)


@pytest.mark.asyncio
async def test_cancelling_latest_cold_connection_does_not_revive_superseded_one(
    tmp_path, monkeypatch,
):
    load = EventOutbox.load_replay_state
    first_entered = threading.Event()
    second_entered = threading.Event()
    release = threading.Event()

    def held_load(**kwargs):
        entered = second_entered if first_entered.is_set() else first_entered
        entered.set()
        assert release.wait(3)
        return load(**kwargs)

    monkeypatch.setattr(EventOutbox, "load_replay_state", staticmethod(held_load))
    manager = WebSocketManager()
    first = _connection(tmp_path / "first")
    second = _connection(tmp_path / "second")
    older = asyncio.create_task(manager.connect(**first))
    assert await asyncio.to_thread(first_entered.wait, 2)
    newer = asyncio.create_task(manager.connect(**second))
    try:
        assert await asyncio.to_thread(second_entered.wait, 2)
        newer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await newer
        release.set()
        with pytest.raises(WebSocketDisconnect) as rejected:
            await older
        assert rejected.value.code == 1012
        assert not manager.iter_sessions()
        assert not manager._connecting_websockets
        _assert_released(first)
        _assert_released(second)
    finally:
        release.set()
        await asyncio.gather(older, newer, return_exceptions=True)


def _delayed_socket():
    incoming = asyncio.Queue()
    incoming.put_nowait({"type": "websocket.connect"})
    awaiting_message = asyncio.Event()
    sent_ack = asyncio.Event()

    async def receive():
        if incoming.empty():
            awaiting_message.set()
        return await incoming.get()

    async def send(message):
        if message["type"] == "websocket.send":
            payload = json.loads(message["text"])
            if payload["type"] == "client.command.ack":
                sent_ack.set()

    websocket = WebSocket(
        {"type": "websocket", "headers": [], "query_string": b"session_id=cold-session"},
        receive=receive, send=send,
    )
    return websocket, incoming, awaiting_message, sent_ack


def _create_conversation_frame():
    return {
        "type": "websocket.receive",
        "text": json.dumps({
            "type": "conversation.create",
            "client_command_id": "old-input-command",
            "conversation_id": "conv_old_input",
            "title": "Older connection command",
            "conversation_type": "side_chat",
        }),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("frame_kind", ["durable_command", "ping", "invalid_json"])
async def test_replaced_asgi_reader_cannot_admit_late_input(tmp_path, frame_kind):
    first = _connection(tmp_path / "first")
    newer = _connection(tmp_path / "newer")
    websocket, incoming, awaiting_message, _sent_ack = _delayed_socket()
    first["websocket"] = websocket
    manager = WebSocketManager()
    session, generation = await manager.connect(**first)
    reader = asyncio.create_task(session.command_dispatcher.run(generation))
    try:
        await asyncio.wait_for(awaiting_message.wait(), 2)
        await manager.connect(**newer)
        if frame_kind == "durable_command":
            frame = _create_conversation_frame()
        else:
            frame = {
                "type": "websocket.receive",
                "text": json.dumps({"type": "ping"}) if frame_kind == "ping" else "{bad json",
            }
        incoming.put_nowait(frame)
        await asyncio.wait_for(reader, 2)
        await asyncio.gather(*tuple(session.command_dispatcher.command_tasks))
        await session.event_outbox.drain_persistence()
        assert newer["websocket"].sent == []
        assert session.conversation_repo.get_conversation("conv_old_input") is None
        assert "old-input-command" not in session.command_dispatcher.recent_client_command_id_set
        assert not session.run_manager.durable_queue.has_client_command("old-input-command")
    finally:
        if not reader.done():
            reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)
        await _release_session(session)


@pytest.mark.asyncio
async def test_replacement_preserves_work_already_durably_admitted(tmp_path):
    first = _connection(tmp_path / "first")
    newer = _connection(tmp_path / "newer")
    websocket, incoming, _awaiting_message, sent_ack = _delayed_socket()
    first["websocket"] = websocket
    manager = WebSocketManager()
    session, generation = await manager.connect(**first)
    session.command_dispatcher.command_semaphore = asyncio.Semaphore(0)
    reader = asyncio.create_task(session.command_dispatcher.run(generation))
    try:
        incoming.put_nowait(_create_conversation_frame())
        await asyncio.wait_for(sent_ack.wait(), 2)
        assert session.run_manager.durable_queue.has_client_command("old-input-command")
        await manager.connect(**newer)
        session.command_dispatcher.command_semaphore.release()
        await asyncio.gather(*tuple(session.command_dispatcher.command_tasks))
        assert session.conversation_repo.get_conversation("conv_old_input") is not None
        assert "old-input-command" in session.command_dispatcher.recent_client_command_id_set
        assert not session.run_manager.durable_queue.has_client_command("old-input-command")
        incoming.put_nowait({"type": "websocket.disconnect", "code": 1000})
        with pytest.raises(WebSocketDisconnect):
            await reader
    finally:
        if not reader.done():
            reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)
        await _release_session(session)
