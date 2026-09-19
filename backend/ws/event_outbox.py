"""WebSocket event delivery, persistence, and reconnect replay ownership."""

from __future__ import annotations

import asyncio
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import logging
import uuid
from pathlib import Path
from typing import Any, Callable, Collection, Iterator

from fastapi import WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState
from websockets.exceptions import ConnectionClosed

from backend.async_cleanup import (
    CANCELLATION_DRAIN_TIMEOUT_SECONDS,
    await_with_deadline,
)
from backend.ws.event_log import (
    WebSocketReplayEventStore,
    is_hidden_provider_reasoning_event,
    is_raw_provider_reasoning_event,
    sanitize_ws_live_payload,
    sanitize_ws_replay_payload,
)
from backend.ws.payload_contracts import (
    is_non_replayable_event_type,
    validate_session_projection_payload,
)

logger = logging.getLogger(__name__)

ReplayState = tuple[WebSocketReplayEventStore, list[dict[str, Any]]]

# Upper bound on how many staged events one drain window writes together. The
# window is bounded so a long backlog cannot hold an unbounded line buffer, and
# the remainder is written by the next iteration of the same writer.
_PERSISTENCE_BATCH_LIMIT = 256


class EventOutbox:
    """Own ordered WebSocket delivery and the durable reconnect window."""

    def __init__(
        self,
        *,
        session_id: str,
        websocket: WebSocket,
        replay_root: Path,
        replay_limit: int,
        cleanup_tasks: set[asyncio.Task[Any]],
        has_active_run: Callable[[], bool],
        requires_conversation_owner: Callable[[str, dict[str, Any]], bool],
        workspace_scoped_event_types: Collection[str],
        replay_state: ReplayState | None = None,
    ) -> None:
        self.session_id = session_id
        self.websocket = websocket
        self.connection_generation = 1
        self.connected = True
        self.events_dropped_during_disconnect = False
        self._instance_id = uuid.uuid4().hex
        self._replay_limit = replay_limit
        self._cleanup_tasks = cleanup_tasks
        self._has_active_run = has_active_run
        self._requires_conversation_owner = requires_conversation_owner
        self._workspace_scoped_event_types = workspace_scoped_event_types
        self._store, self._events = (
            replay_state
            if replay_state is not None
            else self.load_replay_state(
                session_id=session_id,
                replay_root=replay_root,
                replay_limit=replay_limit,
            )
        )
        self._event_seq = self._max_replay_event_seq(self._events)
        self._events = deque(self._events, maxlen=replay_limit)
        self._events_since_rewrite = len(self._events)
        self._replay_cursor = self._event_seq
        self._send_lock = asyncio.Lock()
        self._delivery_queue: asyncio.Queue[tuple[int, dict[str, Any], asyncio.Future[bool] | None]] = asyncio.Queue(maxsize=256)
        self._delivery_task: asyncio.Task[None] | None = None
        self._persist_tail: asyncio.Task[None] | None = None
        self._pending_persistence: deque[
            tuple[dict[str, Any], list[dict[str, Any]] | None]
        ] = deque()
        self._persistence_errors: list[dict[str, Any]] = []
        self._persistence_failed_seqs: set[int] = set()
        self._event_generation: ContextVar[int | None] = ContextVar(
            f"ws_event_generation_{session_id}",
            default=None,
        )
        self._client_command_id: ContextVar[str] = ContextVar(
            f"ws_client_command_{session_id}",
            default="",
        )
        self._client_command_type: ContextVar[str] = ContextVar(
            f"ws_client_command_type_{session_id}",
            default="",
        )

    @staticmethod
    def load_replay_state(
        *,
        session_id: str,
        replay_root: Path,
        replay_limit: int,
    ) -> ReplayState:
        store = WebSocketReplayEventStore(session_id=session_id, root_dir=replay_root)
        return store, store.load(limit=replay_limit)

    @property
    def current_replay_seq(self) -> int:
        return self._replay_cursor

    @property
    def replay_log_degraded(self) -> bool:
        """Whether loading the durable replay window lost evidence."""

        return self._store.read_status.degraded

    @property
    def client_command_id(self) -> str:
        return self._client_command_id.get()

    @property
    def persistence_tail(self) -> asyncio.Task[None] | None:
        return self._persist_tail

    @property
    def replay_path(self) -> Path:
        return self._store.path

    @property
    def replay_root(self) -> Path:
        return self._store.root_dir

    def runtime_snapshot(self) -> dict[str, Any]:
        return {
            "log_read_status": self._store.read_status.to_payload(),
            "persistence_failed_sequences": sorted(self._persistence_failed_seqs),
            "persistence_errors": list(self._persistence_errors[-20:]),
            "pending_delivery": self._delivery_queue.qsize(),
        }

    def load_persisted_window(
        self,
        *,
        limit: int,
    ) -> tuple[list[dict[str, Any]], Any]:
        events = self._store.load(limit=limit)
        return events, self._store.read_status

    def attach_websocket(self, websocket: WebSocket) -> tuple[WebSocket, int]:
        previous = self.websocket
        self.websocket = websocket
        self.connection_generation += 1
        self.connected = True
        return previous, self.connection_generation

    def mark_disconnected(self) -> None:
        self.connected = False

    def clear_disconnect_drop_marker(self) -> None:
        self.events_dropped_during_disconnect = False

    @contextmanager
    def bind_client_command(self, command_id: str, command_type: str) -> Iterator[None]:
        command_token = self._client_command_id.set(command_id)
        type_token = self._client_command_type.set(command_type)
        try:
            yield
        finally:
            self._client_command_type.reset(type_token)
            self._client_command_id.reset(command_token)

    @contextmanager
    def bind_connection_generation(self, generation: int | None) -> Iterator[None]:
        token = self._event_generation.set(generation)
        try:
            yield
        finally:
            self._event_generation.reset(token)

    def _resolved_generation(self) -> int:
        generation = self._event_generation.get()
        return self.connection_generation if generation is None else generation

    def _can_send(self, generation: int) -> bool:
        if generation != self.connection_generation or not self.connected:
            return False
        application_state = getattr(self.websocket, "application_state", None)
        client_state = getattr(self.websocket, "client_state", None)
        return (
            application_state != WebSocketState.DISCONNECTED
            and client_state != WebSocketState.DISCONNECTED
        )

    @staticmethod
    def is_expected_disconnect_exception(exc: Exception) -> bool:
        if isinstance(exc, (WebSocketDisconnect, ConnectionClosed)):
            return True
        if isinstance(exc, RuntimeError):
            message = str(exc).lower()
            return (
                "websocket is not connected" in message
                or "close message has been sent" in message
                or "after sending websocket.close" in message
                or 'cannot call "send"' in message
            )
        return False

    async def send_payload(
        self,
        payload: dict[str, Any],
        *,
        connection_generation: int | None = None,
        log_context: str,
        envelope: bool = True,
        wait_for_delivery: bool = True,
    ) -> bool:
        generation = (
            self._resolved_generation()
            if connection_generation is None
            else connection_generation
        )
        payload = dict(payload)
        event_type = str(payload.get("type") or "").strip()
        command_id = self._client_command_id.get()
        command_type = self._client_command_type.get()
        if command_id:
            payload.setdefault("client_command_id", command_id)
        if command_type:
            payload.setdefault("client_command_type", command_type)
        try:
            validate_session_projection_payload(payload)
        except ValueError as exc:
            logger.warning(
                "Dropping invalid session/conversation websocket payload before sanitization: "
                "type=%s session=%s error=%s",
                event_type,
                self.session_id,
                exc,
            )
            return False
        payload = sanitize_ws_live_payload(payload)
        event_type = str(payload.get("type") or "").strip()
        if is_hidden_provider_reasoning_event(payload):
            logger.warning(
                "Dropping hidden provider reasoning websocket payload: type=%s session=%s",
                event_type,
                self.session_id,
            )
            return False
        if (
            self._requires_conversation_owner(event_type, payload)
            and not str(payload.get("conversation_id") or "").strip()
        ):
            logger.warning(
                "Dropping conversation-scoped payload without conversation_id: type=%s session=%s keys=%s",
                event_type,
                self.session_id,
                sorted(payload.keys()),
            )
            return False
        if (
            event_type in self._workspace_scoped_event_types
            and not str(payload.get("workspace_root") or "").strip()
        ):
            logger.warning(
                "Dropping workspace-scoped payload without workspace_root: type=%s session=%s keys=%s",
                event_type,
                self.session_id,
                sorted(payload.keys()),
            )
            return False
        async with self._send_lock:
            enveloped = self._envelope(payload) if envelope else dict(payload)
            if self._is_replayable(enveloped):
                replay_payload, rewrite_events = self._stage(enveloped)
                if rewrite_events is not None:
                    self._pending_persistence.clear()
                self._pending_persistence.append((replay_payload, rewrite_events))
                if self._persist_tail is None or self._persist_tail.done():
                    self._persist_tail = asyncio.create_task(self._persist_pending())
            if not self._can_send(generation):
                if self._has_active_run():
                    self.events_dropped_during_disconnect = True
                logger.debug(
                    "Skipping %s for stale or disconnected websocket in session %s",
                    log_context,
                    self.session_id,
                )
                return False
            receipt = asyncio.get_running_loop().create_future() if wait_for_delivery else None
            if receipt is not None:
                receipt.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
            await self._delivery_queue.put((generation, enveloped, receipt))
            if self._delivery_task is None or self._delivery_task.done():
                self._delivery_task = asyncio.create_task(self._deliver_pending())
        if receipt is not None:
            return await asyncio.shield(receipt)
        # Let an available transport make progress without tying execution to
        # socket completion. The bounded queue owns backpressure for slow peers.
        await asyncio.sleep(0)
        return True

    async def _deliver_pending(self) -> None:
        while not self._delivery_queue.empty():
            generation, payload, receipt = self._delivery_queue.get_nowait()
            delivered = False
            try:
                if self._can_send(generation):
                    await self.websocket.send_json(payload)
                    delivered = True
            except Exception as exc:
                if generation == self.connection_generation:
                    self.connected = False
                if not self.is_expected_disconnect_exception(exc):
                    if receipt is not None:
                        receipt.set_exception(exc)
                    else:
                        logger.exception("Websocket notification delivery failed for %s", self.session_id)
            finally:
                if not delivered and self._has_active_run():
                    self.events_dropped_during_disconnect = True
                if receipt is not None and not receipt.done():
                    receipt.set_result(delivered)
                self._delivery_queue.task_done()

    async def drain_delivery(self) -> None:
        task = self._delivery_task
        if task is not None and not task.done():
            await await_with_deadline(
                task, timeout=CANCELLATION_DRAIN_TIMEOUT_SECONDS,
                label="websocket notification delivery", owner=self._cleanup_tasks,
            )

    def _envelope(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._event_seq += 1
        seq = self._event_seq
        enveloped = dict(payload)
        enveloped["seq"] = seq
        enveloped.setdefault("event_id", f"{self.session_id}:{self._instance_id}:{seq}")
        enveloped.setdefault(
            "timestamp",
            datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        )
        return enveloped

    @staticmethod
    def _is_replayable(payload: dict[str, Any]) -> bool:
        event_type = str(payload.get("type") or "").strip()
        if is_non_replayable_event_type(event_type):
            return False
        # Raw provider reasoning is a live-only projection. The replay store
        # filters it as well, but excluding it here keeps the in-memory window
        # and its replay cursor consistent during same-process reconnects.
        if is_raw_provider_reasoning_event(payload):
            return False
        return bool(str(payload.get("conversation_id") or "").strip())

    def _stage(
        self,
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any], list[dict[str, Any]] | None]:
        payload["previous_replay_seq"] = self._replay_cursor
        replay_payload = sanitize_ws_replay_payload(dict(payload))
        self._replay_cursor = int(replay_payload["seq"])
        self._events.append(replay_payload)
        rewrite_events: list[dict[str, Any]] | None = None
        self._events_since_rewrite += 1
        if self._events_since_rewrite >= self._replay_limit:
            rewrite_events = [dict(event) for event in self._events]
            self._events_since_rewrite = 0
        return replay_payload, rewrite_events

    async def _persist_pending(self) -> None:
        while self._pending_persistence:
            # Yield once so every event staged during this event-loop tick joins
            # the same append. A streamed turn stages hundreds of events; one
            # write per drain window replaces one filesystem round trip each.
            await asyncio.sleep(0)
            batch: list[dict[str, Any]] = []
            while (
                self._pending_persistence
                and len(batch) < _PERSISTENCE_BATCH_LIMIT
            ):
                replay_payload, rewrite_events = self._pending_persistence.popleft()
                if rewrite_events is not None:
                    # Publish queued events first: a rewrite replaces the whole
                    # file with the retained window, so writing the batch after
                    # it would duplicate what the window already contains. The
                    # payload rides along only as the failure trigger, since the
                    # window already carries it.
                    if batch:
                        await self._persist_batch(batch)
                        batch = []
                    await self._persist_batch([replay_payload], rewrite_events)
                    continue
                batch.append(replay_payload)
            if batch:
                await self._persist_batch(batch)

    async def _persist_batch(
        self,
        replay_payloads: list[dict[str, Any]],
        rewrite_events: list[dict[str, Any]] | None = None,
    ) -> None:
        if not replay_payloads and rewrite_events is None:
            return
        # The event whose publication failed is the repair trigger. If the
        # replacement rewrite also fails, only that event is unresolved: the
        # rest of the window is either already on disk or still queued.
        trigger = replay_payloads[0] if replay_payloads else None
        if self._persistence_failed_seqs and rewrite_events is None:
            # Only a failed publication needs a complete repair window. It
            # covers queued events too, so publish that window once in order.
            rewrite_events = [dict(event) for event in self._events]
            self._pending_persistence.clear()
            replay_payloads = []
        try:
            if rewrite_events is not None:
                repaired_events = rewrite_events
                await asyncio.to_thread(self._store.rewrite, repaired_events)
                self._persistence_failed_seqs.difference_update(
                    seq
                    for event in repaired_events
                    if (seq := self._replay_seq_value(event)) is not None
                )
            else:
                await asyncio.to_thread(self._store.append_many, replay_payloads)
                for replay_payload in replay_payloads:
                    seq = self._replay_seq_value(replay_payload)
                    if seq is not None:
                        self._persistence_failed_seqs.discard(seq)
        except Exception as exc:
            if rewrite_events is not None:
                trigger_seq = (
                    self._replay_seq_value(trigger) if trigger is not None else None
                )
                seqs = [] if trigger_seq is None else [trigger_seq]
            else:
                seqs = [
                    seq
                    for seq in (
                        self._replay_seq_value(payload)
                        for payload in replay_payloads
                    )
                    if seq is not None
                ]
            self._persistence_failed_seqs.update(seqs)
            self._persistence_errors.append(
                {
                    "kind": "websocket_replay_persistence",
                    "session_id": self.session_id,
                    "seq": seqs[0] if len(seqs) == 1 else None,
                    "seqs": seqs,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "recorded_at": datetime.now(timezone.utc)
                    .isoformat()
                    .replace("+00:00", "Z"),
                }
            )
            del self._persistence_errors[:-20]
            logger.error(
                "Failed to persist websocket replay event(s) for session %s (seqs=%s)",
                self.session_id,
                seqs,
                exc_info=True,
            )
        finally:
            # An in-flight write may fail after its event has left the retained
            # window. Keep failures for that window; the diagnostic log retains
            # older failures without permanently poisoning replay health.
            if self._persistence_failed_seqs:
                self._persistence_failed_seqs.intersection_update(event["seq"] for event in self._events)

    async def drain_persistence(self) -> None:
        tail = self._persist_tail
        if tail is None or tail.done() or tail is asyncio.current_task():
            return
        try:
            await await_with_deadline(
                tail,
                timeout=CANCELLATION_DRAIN_TIMEOUT_SECONDS,
                label="websocket replay persistence",
                owner=self._cleanup_tasks,
            )
        except Exception:
            logger.debug(
                "Failed to drain websocket replay persistence for session %s",
                self.session_id,
                exc_info=True,
            )

    async def delete_conversation_events(self, conversation_id: str) -> int:
        owner = str(conversation_id or "").strip()
        if not owner:
            return 0
        async with self._send_lock:
            if self._persist_tail is not None:
                await asyncio.shield(self._persist_tail)
            for event in self._events:
                if str(event.get("conversation_id") or "").strip() != owner:
                    continue
                sequence = int(event.get("seq") or 0)
                if sequence in self._persistence_failed_seqs:
                    raise RuntimeError(
                        f"Replay event {sequence} for conversation {owner} "
                        f"was staged but never persisted"
                    )
            removed = await asyncio.to_thread(
                self._store.delete_for_conversation,
                owner,
            )
            self._events = deque((
                event
                for event in self._events
                if str(event.get("conversation_id") or "").strip() != owner
            ), maxlen=self._replay_limit)
            return int(removed)

    @staticmethod
    def _max_replay_event_seq(events: list[dict[str, Any]]) -> int:
        max_seq = 0
        for payload in events:
            value = payload.get("seq")
            if isinstance(value, int) and not isinstance(value, bool):
                max_seq = max(max_seq, value)
        return max_seq

    @staticmethod
    def _replay_seq_value(
        payload: dict[str, Any],
        field: str = "seq",
    ) -> int | None:
        value = payload.get(field)
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        if value <= 0 or value > 9_007_199_254_740_991:
            return None
        return value

    def replay_window_after(
        self,
        last_seq: int,
    ) -> tuple[list[dict[str, Any]], bool]:
        current_seq = self._replay_cursor
        if last_seq <= 0:
            return [], False
        if last_seq > current_seq:
            return [], True
        if last_seq == current_seq:
            return [], False
        if not self._events:
            return [], True

        first_after_index: int | None = None
        for index, payload in enumerate(self._events):
            seq = self._replay_seq_value(payload)
            if seq is not None and seq > last_seq:
                first_after_index = index
                break
        if first_after_index is None:
            return [], True

        expected_previous = last_seq
        materialized: list[dict[str, Any]] = []
        events = list(self._events)
        for index in range(first_after_index, len(events)):
            payload = events[index]
            seq = self._replay_seq_value(payload)
            if seq is None or seq <= expected_previous:
                return [], True
            if seq in self._persistence_failed_seqs:
                return [], True
            if "previous_replay_seq" in payload:
                previous_replay_seq = self._replay_seq_value(
                    payload,
                    "previous_replay_seq",
                )
                if payload.get("previous_replay_seq") == 0:
                    previous_replay_seq = 0
                if index == first_after_index:
                    # The renderer's cursor may sit on a live-only event, which
                    # is never staged (payload_contracts.LIVE_ONLY_EVENT_TYPES).
                    # The next staged event then chains back past that cursor, so
                    # equality would report a gap for every reconnect that
                    # happens mid-stream. Only a chain that would skip a
                    # *persisted* event is a real gap: that is the eviction case,
                    # where the previous persisted sequence is still ahead of the
                    # cursor. Re-anchor the chain below so the session.replay
                    # contract still validates from `last_seq`.
                    if previous_replay_seq is None or previous_replay_seq > expected_previous:
                        return [], True
                elif previous_replay_seq != expected_previous:
                    return [], True
            elif index == first_after_index:
                if index <= 0:
                    return [], True
                retained_previous = self._replay_seq_value(events[index - 1])
                if retained_previous is None or retained_previous > expected_previous:
                    return [], True

            replay_event = dict(payload)
            replay_event["previous_replay_seq"] = expected_previous
            materialized.append(replay_event)
            expected_previous = seq

        if expected_previous != current_seq:
            return [], True
        return materialized, False

    async def replay_missed_events(
        self,
        last_seq: int,
        *,
        events: list[dict[str, Any]] | None = None,
        current_seq: int | None = None,
    ) -> int:
        if events is None:
            events, has_gap = self.replay_window_after(last_seq)
            if has_gap:
                return 0
        else:
            events = [dict(event) for event in events]
        if not events:
            return 0
        for event in events:
            event["replayed"] = True
        sent = await self.send_payload(
            {
                "type": "session.replay",
                "last_seq": last_seq,
                "current_seq": self._replay_cursor if current_seq is None else current_seq,
                "replayed_events": len(events),
                "events": events,
            },
            log_context="session.replay",
        )
        return len(events) if sent else 0
