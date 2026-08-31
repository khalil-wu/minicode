"""WebSocket event delivery, persistence, and reconnect replay ownership."""

from __future__ import annotations

import asyncio
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
    is_raw_provider_reasoning_event,
    sanitize_ws_live_payload,
    sanitize_ws_replay_payload,
)
from backend.ws.payload_contracts import (
    is_non_replayable_event_type,
    validate_session_projection_payload,
)

logger = logging.getLogger(__name__)


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
        self._store = WebSocketReplayEventStore(
            session_id=session_id,
            root_dir=replay_root,
        )
        self._events: list[dict[str, Any]] = self._store.load(limit=replay_limit)
        self._event_seq = self._max_replay_event_seq(self._events)
        self._replay_cursor = self._event_seq
        self._send_lock = asyncio.Lock()
        self._persist_tail: asyncio.Task[None] | None = None
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

    @property
    def current_replay_seq(self) -> int:
        return self._replay_cursor

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
        if is_raw_provider_reasoning_event(payload):
            logger.warning(
                "Dropping raw provider reasoning websocket payload: type=%s session=%s",
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
                persist_snapshot = [dict(event) for event in self._events]
                persist_task = asyncio.create_task(
                    self._persist_after(
                        self._persist_tail,
                        replay_payload,
                        rewrite_events,
                        persist_snapshot,
                    )
                )
                self._persist_tail = persist_task
            if not self._can_send(generation):
                if self._has_active_run():
                    self.events_dropped_during_disconnect = True
                logger.debug(
                    "Skipping %s for stale or disconnected websocket in session %s",
                    log_context,
                    self.session_id,
                )
                return False
            try:
                await self.websocket.send_json(enveloped)
            except Exception as exc:
                if not self.is_expected_disconnect_exception(exc):
                    raise
                if self._has_active_run():
                    self.events_dropped_during_disconnect = True
                logger.debug(
                    "Dropping %s after websocket disconnect in session %s: %s",
                    log_context,
                    self.session_id,
                    exc,
                )
                return False
            return True

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
        if len(self._events) > self._replay_limit:
            del self._events[: len(self._events) - self._replay_limit]
            rewrite_events = [dict(event) for event in self._events]
        return replay_payload, rewrite_events

    async def _persist_after(
        self,
        previous: asyncio.Task[None] | None,
        replay_payload: dict[str, Any],
        rewrite_events: list[dict[str, Any]] | None,
        persist_snapshot: list[dict[str, Any]],
    ) -> None:
        predecessor_failed = False
        if previous is not None:
            try:
                await asyncio.shield(previous)
            except asyncio.CancelledError:
                predecessor_failed = True
            except Exception:
                predecessor_failed = True
                logger.debug(
                    "Repairing websocket replay persistence after a failed predecessor",
                    exc_info=True,
                )
        try:
            if rewrite_events is not None or predecessor_failed:
                repaired_events = (
                    rewrite_events if rewrite_events is not None else persist_snapshot
                )
                await asyncio.to_thread(self._store.rewrite, repaired_events)
                self._persistence_failed_seqs.difference_update(
                    seq
                    for event in repaired_events
                    if (seq := self._replay_seq_value(event)) is not None
                )
            else:
                await asyncio.to_thread(self._store.append, replay_payload)
                seq = self._replay_seq_value(replay_payload)
                if seq is not None:
                    self._persistence_failed_seqs.discard(seq)
        except Exception as exc:
            seq = self._replay_seq_value(replay_payload)
            if seq is not None:
                self._persistence_failed_seqs.add(seq)
            self._persistence_errors.append(
                {
                    "kind": "websocket_replay_persistence",
                    "session_id": self.session_id,
                    "seq": seq,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "recorded_at": datetime.now(timezone.utc)
                    .isoformat()
                    .replace("+00:00", "Z"),
                }
            )
            del self._persistence_errors[:-20]
            logger.error(
                "Failed to persist websocket replay event for session %s (seq=%s)",
                self.session_id,
                seq,
                exc_info=True,
            )

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
            await self.drain_persistence()
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
            self._events = [
                event
                for event in self._events
                if str(event.get("conversation_id") or "").strip() != owner
            ]
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
        for index in range(first_after_index, len(self._events)):
            payload = self._events[index]
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
                if previous_replay_seq != expected_previous:
                    return [], True
            elif index == first_after_index:
                if index <= 0:
                    return [], True
                retained_previous = self._replay_seq_value(self._events[index - 1])
                if retained_previous != expected_previous:
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
