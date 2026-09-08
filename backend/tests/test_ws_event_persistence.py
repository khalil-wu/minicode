from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
from types import SimpleNamespace
import threading

import pytest

from backend.ws import event_outbox as outbox_module
from backend.ws.event_outbox import EventOutbox
from backend.ws.handlers import conversation as conversation_handlers


class RecordingSocket:
    def __init__(self):
        self.events = []

    async def send_json(self, payload):
        self.events.append(dict(payload))


@pytest.fixture
def outbox(tmp_path):
    return EventOutbox(
        session_id="persistence-session",
        websocket=RecordingSocket(),
        replay_root=tmp_path,
        replay_limit=100,
        cleanup_tasks=set(),
        has_active_run=lambda: False,
        requires_conversation_owner=lambda *_args: True,
        workspace_scoped_event_types=(),
    )


async def _send(outbox, text, *, conversation_id="conversation"):
    assert await outbox.send_payload({
        "type": "agent_message.delta",
        "conversation_id": conversation_id,
        "item_id": "message",
        "delta": text,
    }, log_context="test")


def _persisted_sequences(outbox):
    return [event["seq"] for event in outbox._store.load(limit=100)]


@contextmanager
def _unwritable_log(outbox):
    path = outbox.replay_path
    backup = path.with_suffix(".backup")
    path.rename(backup)
    path.mkdir()
    try:
        yield
    finally:
        path.rmdir()
        backup.rename(path)


@asynccontextmanager
async def _pending_writes(outbox, monkeypatch):
    await _send(outbox, "first")
    await outbox.persistence_tail
    append = outbox._store.append
    started = threading.Event()
    release = threading.Event()

    def blocked_append(payload):
        if payload["seq"] == 2:
            started.set()
            assert release.wait(timeout=5)
        append(payload)

    monkeypatch.setattr(outbox._store, "append", blocked_append)
    try:
        await _send(outbox, "second")
        assert await asyncio.to_thread(started.wait, 2)
        await _send(outbox, "third")
        yield release
    finally:
        release.set()
        await outbox.persistence_tail


@pytest.mark.parametrize("failed_count", [1, 2])
def test_next_write_repairs_real_failed_appends_from_retained_events(outbox, failed_count):
    async def scenario():
        await _send(outbox, "first")
        await outbox.persistence_tail
        with _unwritable_log(outbox):
            for index in range(failed_count):
                await _send(outbox, f"failed-{index}")
                await outbox.persistence_tail
        failure = outbox.runtime_snapshot()
        assert failure["persistence_failed_sequences"] == list(range(2, failed_count + 2))
        assert {error["kind"] for error in failure["persistence_errors"]} == {"websocket_replay_persistence"}

        await _send(outbox, "recovered")
        await outbox.drain_persistence()

        expected = list(range(1, failed_count + 3))
        assert _persisted_sequences(outbox) == expected
        assert [event["seq"] for event in outbox.websocket.events] == expected
        assert outbox.runtime_snapshot()["persistence_failed_sequences"] == []
        replay, has_gap = outbox.replay_window_after(1)
        assert not has_gap
        assert [event["seq"] for event in replay] == expected[1:]
        assert [event["previous_replay_seq"] for event in replay] == expected[:-1]
        assert len(outbox.runtime_snapshot()["persistence_errors"]) == failed_count

    asyncio.run(scenario())


def test_repair_deduplicates_an_append_that_failed_after_its_write(outbox, monkeypatch):
    append = outbox._store.append

    def fail_after_write(payload):
        append(payload)
        if payload["seq"] == 2:
            raise OSError("failure after the file was written")

    monkeypatch.setattr(outbox._store, "append", fail_after_write)

    async def scenario():
        for text in ("first", "second", "third"):
            await _send(outbox, text)
            await outbox.persistence_tail
        assert _persisted_sequences(outbox) == [1, 2, 3]
        assert outbox.runtime_snapshot()["persistence_failed_sequences"] == []

    asyncio.run(scenario())


def test_delete_refuses_an_unrepaired_owned_event(outbox):
    async def scenario():
        await _send(outbox, "first")
        await outbox.persistence_tail
        with _unwritable_log(outbox):
            await _send(outbox, "failed")
            await outbox.persistence_tail

        with pytest.raises(RuntimeError, match="staged but never persisted"):
            await outbox.delete_conversation_events("conversation")

        assert _persisted_sequences(outbox) == [1]
        assert [event["seq"] for event in outbox._events] == [1, 2]
        assert outbox.runtime_snapshot()["persistence_failed_sequences"] == [2]
        await _send(outbox, "recovered")
        assert await outbox.delete_conversation_events("conversation") == 3
        assert _persisted_sequences(outbox) == []

    asyncio.run(scenario())


def test_delete_preserves_another_conversations_failed_event(outbox):
    async def scenario():
        await _send(outbox, "first")
        await outbox.persistence_tail
        with _unwritable_log(outbox):
            await _send(outbox, "failed", conversation_id="other")
            await outbox.persistence_tail

        assert await outbox.delete_conversation_events("conversation") == 1
        assert outbox.runtime_snapshot()["persistence_failed_sequences"] == [2]
        assert [event["seq"] for event in outbox._events] == [2]
        await _send(outbox, "recovered", conversation_id="other")
        await outbox.persistence_tail
        assert _persisted_sequences(outbox) == [2, 3]
        assert outbox.runtime_snapshot()["persistence_failed_sequences"] == []

    asyncio.run(scenario())


def test_delete_handler_retains_its_barrier_until_writers_finish(outbox, monkeypatch):
    monkeypatch.setattr(outbox_module, "CANCELLATION_DRAIN_TIMEOUT_SECONDS", 0.005)
    monkeypatch.setattr(conversation_handlers, "CANCELLATION_DRAIN_TIMEOUT_SECONDS", 0.03)
    session = SimpleNamespace(
        session_id=outbox.session_id,
        ws_manager=None,
        cleanup_tasks=set(),
        event_outbox=outbox,
    )

    async def scenario():
        async with _pending_writes(outbox, monkeypatch) as release:
            counts, errors = await conversation_handlers._purge_conversation_replay_state(
                session, "conversation",
            )
            assert counts == {}
            assert errors == [f"{session.session_id}:replay_events"]
            assert len(session.cleanup_tasks) == 1
            assert _persisted_sequences(outbox) == [1]
            assert not outbox.persistence_tail.done()
            next_send = asyncio.create_task(_send(outbox, "other", conversation_id="other"))
            await asyncio.sleep(0)
            assert not next_send.done()

            release.set()
            assert await asyncio.gather(*tuple(session.cleanup_tasks)) == [3]
            await next_send
            await outbox.persistence_tail
            await asyncio.sleep(0)

            assert session.cleanup_tasks == set()
            assert _persisted_sequences(outbox) == [4]
            assert [event["seq"] for event in outbox._events] == [4]
            assert outbox._events[0]["conversation_id"] == "other"

    asyncio.run(scenario())


def test_cancelling_delete_does_not_cancel_or_overtake_pending_writers(outbox, monkeypatch):
    async def scenario():
        async with _pending_writes(outbox, monkeypatch) as release:
            deletion = asyncio.create_task(outbox.delete_conversation_events("conversation"))
            await asyncio.sleep(0)
            deletion.cancel()
            with pytest.raises(asyncio.CancelledError):
                await deletion

            assert not outbox.persistence_tail.cancelled()
            assert [event["seq"] for event in outbox._events] == [1, 2, 3]
            assert _persisted_sequences(outbox) == [1]
            release.set()
            await outbox.persistence_tail
            assert _persisted_sequences(outbox) == [1, 2, 3]
            assert await outbox.delete_conversation_events("conversation") == 3
            assert _persisted_sequences(outbox) == []

    asyncio.run(scenario())


def test_pending_full_rewrites_coalesce_without_reordering_the_active_write(outbox, monkeypatch):
    outbox._replay_limit = 3
    rewrite = outbox._store.rewrite
    rewrites = []

    def record_rewrite(events):
        rewrites.append([event["seq"] for event in events])
        rewrite(events)

    monkeypatch.setattr(outbox._store, "rewrite", record_rewrite)

    async def scenario():
        async with _pending_writes(outbox, monkeypatch) as release:
            writer = outbox.persistence_tail
            for sequence in range(4, 9):
                await _send(outbox, f"event-{sequence}")

            assert outbox.persistence_tail is writer
            assert not writer.done()
            assert len(outbox._pending_persistence) == 1
            assert [event["seq"] for event in outbox.websocket.events] == list(range(1, 9))
            assert _persisted_sequences(outbox) == [1]
            release.set()
            await writer

            assert rewrites == [[6, 7, 8]]
            assert _persisted_sequences(outbox) == [6, 7, 8]
            replay, has_gap = outbox.replay_window_after(5)
            assert not has_gap
            assert [event["seq"] for event in replay] == [6, 7, 8]

    asyncio.run(scenario())


def test_events_arriving_during_a_rewrite_stay_on_the_same_writer(outbox, monkeypatch):
    outbox._replay_limit = 2
    rewrite = outbox._store.rewrite
    started = threading.Event()
    release = threading.Event()
    rewrites = []

    def blocked_rewrite(events):
        sequences = [event["seq"] for event in events]
        rewrites.append(sequences)
        if sequences == [2, 3]:
            started.set()
            assert release.wait(timeout=5)
        rewrite(events)

    monkeypatch.setattr(outbox._store, "rewrite", blocked_rewrite)

    async def scenario():
        await _send(outbox, "first")
        await _send(outbox, "second")
        await outbox.persistence_tail
        try:
            await _send(outbox, "third")
            assert await asyncio.to_thread(started.wait, 2)
            writer = outbox.persistence_tail
            for sequence in range(4, 7):
                await _send(outbox, f"event-{sequence}")
            assert outbox.persistence_tail is writer
        finally:
            release.set()
            await outbox.persistence_tail
        assert rewrites == [[2, 3], [5, 6]]
        assert _persisted_sequences(outbox) == [5, 6]
        assert [event["seq"] for event in outbox.websocket.events] == list(range(1, 7))

    asyncio.run(scenario())


def test_failed_coalesced_rewrite_is_repaired_by_the_next_retained_window(outbox):
    outbox._replay_limit = 3

    async def scenario():
        await _send(outbox, "first")
        await outbox.persistence_tail
        with _unwritable_log(outbox):
            for sequence in range(2, 6):
                await _send(outbox, f"event-{sequence}")
            await outbox.persistence_tail
        assert outbox.runtime_snapshot()["persistence_failed_sequences"] == [5]
        assert _persisted_sequences(outbox) == [1]

        await _send(outbox, "recovered")
        await outbox.persistence_tail

        assert _persisted_sequences(outbox) == [4, 5, 6]
        assert outbox.runtime_snapshot()["persistence_failed_sequences"] == []
        replay, has_gap = outbox.replay_window_after(3)
        assert not has_gap
        assert [event["seq"] for event in replay] == [4, 5, 6]

    asyncio.run(scenario())
