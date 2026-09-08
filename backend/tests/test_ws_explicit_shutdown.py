from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import threading
from types import SimpleNamespace

import pytest

from backend.tests.test_ws_cold_connection import _connection
from backend.tests.test_ws_connection_handoff import _create_conversation_frame, _delayed_socket
from backend.tests.test_ws_session_retirement import _emit, _start_operation
from backend.ws.manager import WebSocketManager


@asynccontextmanager
async def _closing_session(tmp_path, monkeypatch, *, initiator="session", short_drain=False):
    manager = WebSocketManager()
    session, generation = await manager.connect(**_connection(tmp_path / "first"))
    await _emit(session, "old-1")
    await session.event_outbox.persistence_tail
    append_started = threading.Event()
    release = threading.Event()
    drain_started = asyncio.Event()
    drain_returned = asyncio.Event()
    append = session.event_outbox._store.append
    drain = session.event_outbox.drain_persistence
    pending = set()

    def held_append(payload):
        append_started.set()
        assert release.wait(8)
        append(payload)

    async def observed_drain():
        drain_started.set()
        await drain()
        drain_returned.set()

    monkeypatch.setattr(session.event_outbox._store, "append", held_append)
    monkeypatch.setattr(session.event_outbox, "drain_persistence", observed_drain)
    if short_drain:
        monkeypatch.setattr("backend.ws.event_outbox.CANCELLATION_DRAIN_TIMEOUT_SECONDS", 0.01)
    await _emit(session, "old-2")
    assert await asyncio.to_thread(append_started.wait, 2)
    timer = None
    if initiator == "grace":
        manager.disconnect("cold-session", connection_generation=generation)
        timer = manager._disconnect_tasks["cold-session"]
    request = asyncio.create_task(
        manager.shutdown()
        if initiator == "application"
        else manager.shutdown_session("cold-session", reason="extension_shutdown")
    )
    if initiator == "extension_owner":
        session._extension_requested_shutdown_task = request
        session.command_dispatcher.track_command_task(request)
    try:
        await asyncio.wait_for(drain_started.wait(), 2)
        if short_drain:
            await asyncio.wait_for(drain_returned.wait(), 2)
        yield SimpleNamespace(
            manager=manager, session=session, request=request,
            retirement=manager._disconnect_tasks.get("cold-session"),
            release=release, pending=pending, timer=timer,
        )
    finally:
        release.set()
        await asyncio.gather(request, *pending, return_exceptions=True)
        await manager.shutdown(reason="test_cleanup")


@pytest.mark.asyncio
@pytest.mark.parametrize("initiator", ["session", "application"])
@pytest.mark.parametrize("short_drain", [False, True])
@pytest.mark.parametrize("operation", ["reconnect", "shutdown", "session_shutdown"])
async def test_full_shutdown_keeps_writer_owned_through_new_operations(
    tmp_path, monkeypatch, initiator, short_drain, operation,
):
    async with _closing_session(
        tmp_path, monkeypatch, initiator=initiator, short_drain=short_drain,
    ) as context:
        assert context.manager.get_session("cold-session") is None
        assert context.retirement is not None and not context.retirement.done()
        assert not context.request.done()
        assert not context.session.event_outbox.persistence_tail.done()
        observer = _start_operation(context, tmp_path, operation)
        await asyncio.sleep(0)
        assert not observer.done()
        context.release.set()
        result = await asyncio.wait_for(observer, 2)
        await context.request
        if operation == "reconnect":
            session, generation = result
            assert session is not context.session
            assert generation == 1
            assert session.event_outbox.current_replay_seq == 2
            await _emit(session, "new-3")
            await session.event_outbox.persistence_tail
        events = context.session.event_outbox._store.load(limit=100)
        assert [event["seq"] for event in events] == (
            [1, 2, 3] if operation == "reconnect" else [1, 2]
        )
        assert [event["delta"] for event in events] == (
            ["old-1", "old-2", "new-3"] if operation == "reconnect" else ["old-1", "old-2"]
        )
        assert context.session.llm.close_calls == 1
        assert context.session.artifact_store.flush_calls == 1
        assert context.session.artifact_store.shutdown_calls == 1
        assert context.session.artifact_store.clear_calls == 1
        assert len(context.session.ws.closed) == 1
        assert context.session.ws.closed[0]["code"] == 1000
        assert not context.manager._disconnect_tasks


@pytest.mark.asyncio
@pytest.mark.parametrize("initiator", ["session", "application"])
async def test_cancelling_shutdown_requester_preserves_full_resource_cleanup(
    tmp_path, monkeypatch, initiator,
):
    async with _closing_session(tmp_path, monkeypatch, initiator=initiator) as context:
        context.request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await context.request
        assert context.retirement is context.manager._disconnect_tasks["cold-session"]
        assert context.retirement.cancelling() == 0
        observer = _start_operation(context, tmp_path, "reconnect")
        await asyncio.sleep(0)
        assert not observer.done()
        context.release.set()
        session, _generation = await asyncio.wait_for(observer, 2)
        assert session.event_outbox.current_replay_seq == 2
        assert context.session.artifact_store.flush_calls == 1
        assert context.session.artifact_store.shutdown_calls == 1
        assert len(context.session.ws.closed) == 1
        assert not context.manager._disconnect_tasks


@pytest.mark.asyncio
@pytest.mark.parametrize("initiator", ["grace", "extension_owner"])
async def test_existing_session_tasks_cannot_remove_explicit_shutdown_owner(
    tmp_path, monkeypatch, initiator,
):
    async with _closing_session(tmp_path, monkeypatch, initiator=initiator) as context:
        assert context.retirement is not None and not context.retirement.done()
        if initiator == "grace":
            await asyncio.gather(context.timer, return_exceptions=True)
            assert context.timer.done()
        else:
            assert context.request.cancelled()
        assert context.manager._disconnect_tasks["cold-session"] is context.retirement
        context.release.set()
        await asyncio.wait_for(context.retirement, 2)
        assert context.session.artifact_store.shutdown_calls == 1
        assert len(context.session.ws.closed) == 1
        assert not context.manager._disconnect_tasks


@pytest.mark.asyncio
@pytest.mark.parametrize("awaiting_frame", [False, True])
@pytest.mark.parametrize("frame_kind", ["durable_command", "ping", "invalid_json"])
async def test_closing_session_cannot_admit_more_transport_input(
    tmp_path, monkeypatch, awaiting_frame, frame_kind,
):
    connection = _connection(tmp_path / "first")
    websocket, incoming, awaiting_message, sent_ack = _delayed_socket()
    connection["websocket"] = websocket
    manager = WebSocketManager()
    session, generation = await manager.connect(**connection)
    reader = None
    if awaiting_frame:
        reader = asyncio.create_task(session.command_dispatcher.run(generation))
        await asyncio.wait_for(awaiting_message.wait(), 2)
    drain_started = asyncio.Event()
    release = asyncio.Event()
    drain = session.event_outbox.drain_persistence

    async def held_drain():
        drain_started.set()
        await release.wait()
        await drain()

    monkeypatch.setattr(session.event_outbox, "drain_persistence", held_drain)
    closing = asyncio.create_task(manager.shutdown_session("cold-session"))
    try:
        await asyncio.wait_for(drain_started.wait(), 2)
        if reader is None:
            reader = asyncio.create_task(session.command_dispatcher.run(generation))
        frame = _create_conversation_frame() if frame_kind == "durable_command" else {
            "type": "websocket.receive",
            "text": json.dumps({"type": "ping"}) if frame_kind == "ping" else "{bad json",
        }
        incoming.put_nowait(frame)
        await asyncio.wait_for(reader, 2)
        assert not session.command_dispatcher.command_tasks
        assert not session.command_dispatcher.recent_client_command_id_set
        assert not session.run_manager.durable_queue.has_client_command("old-input-command")
        assert session.conversation_repo.get_conversation("conv_old_input") is None
        assert not sent_ack.is_set()
        assert session.event_outbox.current_replay_seq == 0
    finally:
        release.set()
        if reader is not None and not reader.done():
            reader.cancel()
        await asyncio.gather(closing, *([reader] if reader is not None else []), return_exceptions=True)
