from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
import threading

import pytest

from backend.tests.test_ws_cold_connection import _connection, _release_session
from backend.ws.manager import WebSocketManager


async def _emit(session, text):
    await session.event_outbox.send_payload({
        "type": "agent_message.delta", "conversation_id": "conv_retirement",
        "item_id": "message", "delta": text,
    }, log_context="test")


@asynccontextmanager
async def _retiring_session(tmp_path, monkeypatch, *, short_drain=False):
    manager = WebSocketManager()
    session, generation = await manager.connect(**_connection(tmp_path / "first"))
    await _emit(session, "old-1")
    await session.event_outbox.persistence_tail
    append_started = threading.Event()
    finish_append = threading.Event()
    cleanup_started = asyncio.Event()
    cleanup_returned = asyncio.Event()
    append = session.event_outbox._store.append
    sleep = asyncio.sleep
    pending = set()

    def held_append(payload):
        append_started.set()
        assert finish_append.wait(4)
        append(payload)

    async def grace_elapsed(delay, *args, **kwargs):
        return await sleep(0 if delay == 30.0 else delay, *args, **kwargs)

    async def cleanup(*, reason):
        cleanup_started.set()
        await _release_session(session)
        cleanup_returned.set()

    monkeypatch.setattr(session.event_outbox._store, "append", held_append)
    monkeypatch.setattr(session.session_lifecycle, "shutdown", cleanup)
    monkeypatch.setattr("backend.ws.manager.asyncio.sleep", grace_elapsed)
    if short_drain:
        monkeypatch.setattr("backend.ws.event_outbox.CANCELLATION_DRAIN_TIMEOUT_SECONDS", 0.01)
    await _emit(session, "old-2")
    assert await asyncio.to_thread(append_started.wait, 2)
    manager.disconnect("cold-session", connection_generation=generation)
    retirement = manager._disconnect_tasks["cold-session"]
    await asyncio.wait_for(cleanup_started.wait(), 2)
    try:
        yield SimpleNamespace(
            manager=manager, session=session, retirement=retirement,
            release=finish_append, pending=pending,
            cleanup_returned=cleanup_returned,
        )
    finally:
        finish_append.set()
        await asyncio.gather(retirement, *pending, return_exceptions=True)
        for live_session in manager.iter_sessions():
            await _release_session(live_session)


def _start_operation(context, tmp_path, operation):
    if operation == "reconnect":
        awaitable = context.manager.connect(**_connection(tmp_path / "next"))
    elif operation == "shutdown":
        awaitable = context.manager.shutdown()
    else:
        awaitable = context.manager.shutdown_session("cold-session")
    task = asyncio.create_task(awaitable)
    context.pending.add(task)
    return task


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["reconnect", "shutdown", "session_shutdown"])
async def test_expired_cleanup_remains_owned_until_real_flush(
    tmp_path, monkeypatch, operation,
):
    async with _retiring_session(tmp_path, monkeypatch) as context:
        assert context.manager.get_session("cold-session") is None
        assert context.manager._disconnect_tasks["cold-session"] is context.retirement
        pending = _start_operation(context, tmp_path, operation)
        await asyncio.sleep(0)
        assert not pending.done()
        assert not context.retirement.done()
        context.release.set()
        result = await asyncio.wait_for(pending, 2)
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
        assert context.retirement.done()
        assert "cold-session" not in context.manager._disconnect_tasks


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["reconnect", "shutdown", "session_shutdown"])
async def test_cancelling_a_waiter_does_not_cancel_expired_cleanup(
    tmp_path, monkeypatch, operation,
):
    async with _retiring_session(tmp_path, monkeypatch) as context:
        pending = _start_operation(context, tmp_path, operation)
        await asyncio.sleep(0)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert context.retirement.cancelling() == 0
        assert context.manager._disconnect_tasks["cold-session"] is context.retirement
        assert not context.manager._connecting_websockets
        context.release.set()
        await asyncio.wait_for(context.retirement, 2)
        assert [event["seq"] for event in context.session.event_outbox._store.load(limit=100)] == [1, 2]
        assert not context.manager._disconnect_tasks


@pytest.mark.asyncio
async def test_expired_cleanup_does_not_block_another_renderer(tmp_path, monkeypatch):
    async with _retiring_session(tmp_path, monkeypatch) as context:
        session, generation = await context.manager.connect(
            **_connection(tmp_path / "unrelated", "unrelated-session"),
        )
        assert generation == 1
        assert session.session_id == "unrelated-session"
        assert not context.retirement.done()
        assert context.manager._disconnect_tasks["cold-session"] is context.retirement


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["reconnect", "shutdown", "session_shutdown"])
async def test_retirement_waits_for_writer_after_bounded_drain_timeout(
    tmp_path, monkeypatch, operation,
):
    async with _retiring_session(tmp_path, monkeypatch, short_drain=True) as context:
        await asyncio.wait_for(context.cleanup_returned.wait(), 2)
        assert not context.session.event_outbox.persistence_tail.done()
        assert not context.retirement.done()
        assert context.manager._disconnect_tasks["cold-session"] is context.retirement
        pending = _start_operation(context, tmp_path, operation)
        await asyncio.sleep(0)
        assert not pending.done()
        context.release.set()
        result = await asyncio.wait_for(pending, 2)
        if operation == "reconnect":
            session, _generation = result
            assert session.event_outbox.current_replay_seq == 2
            await _emit(session, "new-3")
            await session.event_outbox.persistence_tail
        events = context.session.event_outbox._store.load(limit=100)
        assert [event["seq"] for event in events] == (
            [1, 2, 3] if operation == "reconnect" else [1, 2]
        )
        assert not context.manager._disconnect_tasks


@pytest.mark.asyncio
async def test_reconnect_before_grace_deadline_reuses_session(tmp_path):
    manager = WebSocketManager()
    session, generation = await manager.connect(**_connection(tmp_path / "first"))
    manager.disconnect("cold-session", connection_generation=generation)
    timer = manager._disconnect_tasks["cold-session"]
    try:
        restored, generation = await manager.connect(**_connection(tmp_path / "next"))
        await asyncio.gather(timer, return_exceptions=True)
        assert restored is session
        assert generation == 2
        assert session.is_connected
        assert not manager._disconnect_tasks
    finally:
        await _release_session(session)
