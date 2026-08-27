"""Turn-local user input queue.

This module owns the small, deterministic boundary between a WebSocket session
and a running agent turn.  A message promoted with ``steer`` is no longer
implemented by cancelling the whole run; it is delivered at the next safe
agent boundary (after the current provider/tool round).

The queue intentionally stores the original command so an input that arrives
too late can be restored to the normal follow-up queue without losing
attachments or message identifiers.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from enum import Enum
from threading import RLock
from time import time
from typing import Any, Literal


TurnInputMode = Literal["steer"]


class MailboxDeliveryPhase(str, Enum):
    """Which turn may consume newly arrived coordination messages."""

    CURRENT_TURN = "current_turn"
    NEXT_TURN = "next_turn"
    SEALED = "sealed"


@dataclass(frozen=True)
class TurnInput:
    mode: TurnInputMode
    content: str
    message_id: str
    user_message_id: str
    attachments: tuple[dict[str, Any], ...]
    selected_skills: tuple[dict[str, str], ...]
    selected_plugins: tuple[dict[str, str], ...]
    target_message_id: str
    original_command: Any
    queued_at_ms: int

    @classmethod
    def from_command(
        cls,
        command: Any,
        *,
        mode: TurnInputMode = "steer",
        target_message_id: str = "",
    ) -> "TurnInput":
        data = getattr(command, "data", {})
        if not isinstance(data, dict):
            data = {}
        attachments = tuple(
            dict(item)
            for item in (data.get("attachments") or [])
            if isinstance(item, dict)
        )
        selected_skills = tuple(
            {
                "name": str(item.get("name") or "").strip(),
                "path": str(item.get("path") or "").strip(),
            }
            for item in (data.get("skills") or [])
            if isinstance(item, dict)
            and str(item.get("name") or "").strip()
            and str(item.get("path") or "").strip()
        )
        selected_plugins = tuple(
            {
                "config_name": str(
                    item.get("config_name")
                    or item.get("configName")
                    or item.get("name")
                    or ""
                ).strip(),
                "path": str(item.get("path") or "").strip(),
            }
            for item in (data.get("plugins") or [])
            if isinstance(item, dict)
            and (
                str(item.get("config_name") or item.get("configName") or item.get("name") or "").strip()
                or str(item.get("path") or "").strip().startswith("plugin://")
            )
        )
        return cls(
            mode=mode,
            content=str(data.get("content") or ""),
            message_id=str(data.get("assistant_message_id") or "").strip(),
            user_message_id=str(data.get("user_message_id") or "").strip(),
            attachments=attachments,
            selected_skills=selected_skills,
            selected_plugins=selected_plugins,
            target_message_id=str(target_message_id or "").strip(),
            original_command=command,
            queued_at_ms=int(time() * 1000),
        )


class TurnInputQueue:
    """Atomic owner for turn-local input and mailbox delivery phase.

    A conversation keeps one instance for the lifetime of its active turn.
    Final answer commit moves mailbox delivery to ``NEXT_TURN`` before the
    visible terminal event is published, so a late child result cannot extend
    an answer the user has already seen.
    """

    def __init__(self) -> None:
        self._steering: deque[TurnInput] = deque()
        self._phase = MailboxDeliveryPhase.CURRENT_TURN
        self._turn_id = ""
        self._turn_epoch = 0
        self._activity_seq = 0
        self._activity_waiters: set[
            tuple[asyncio.AbstractEventLoop, asyncio.Event]
        ] = set()
        self._lock = RLock()

    def _notify_activity_waiters(
        self,
        waiters: tuple[tuple[asyncio.AbstractEventLoop, asyncio.Event], ...],
    ) -> None:
        for loop, event in waiters:
            try:
                loop.call_soon_threadsafe(event.set)
            except RuntimeError:
                # The owning turn/event loop was already sealed.  The waiter
                # removes itself in its finally block.
                continue

    def activity_cursor(self) -> int:
        """Return the monotonic steer-enqueue sequence."""

        with self._lock:
            return self._activity_seq

    async def wait_for_activity(
        self,
        *,
        after_seq: int,
        timeout: float,
    ) -> bool:
        """Wait for user steer input without consuming it.

        Activity observers wake on new user input without consuming it; the
        normal turn-boundary consumer remains authoritative. The loop/event
        pair keeps the synchronous WebSocket enqueue path thread-safe.
        """

        cursor = max(0, int(after_seq))
        loop = asyncio.get_running_loop()
        event = asyncio.Event()
        waiter = (loop, event)
        with self._lock:
            if self._activity_seq > cursor:
                return True
            self._activity_waiters.add(waiter)
            if self._activity_seq > cursor:
                self._activity_waiters.discard(waiter)
                return True
        try:
            await asyncio.wait_for(event.wait(), timeout=max(0.0, float(timeout)))
            with self._lock:
                return self._activity_seq > cursor
        except asyncio.TimeoutError:
            return False
        finally:
            with self._lock:
                self._activity_waiters.discard(waiter)

    @property
    def sealed(self) -> bool:
        with self._lock:
            return self._phase is MailboxDeliveryPhase.SEALED

    @property
    def mailbox_phase(self) -> MailboxDeliveryPhase:
        with self._lock:
            return self._phase

    @property
    def turn_epoch(self) -> int:
        with self._lock:
            return self._turn_epoch

    @property
    def turn_id(self) -> str:
        with self._lock:
            return self._turn_id

    def begin_turn(self, turn_id: str) -> int:
        """Bind this owner to one active turn and return its epoch.

        Every call marks a turn beginning, so current-turn mailbox delivery is
        always (re)opened.  Re-registering the same run keeps the epoch stable;
        a different run advances it.
        """
        normalized = str(turn_id or "").strip()
        with self._lock:
            same_run = (
                bool(normalized)
                and normalized == self._turn_id
                and self._phase is not MailboxDeliveryPhase.SEALED
            )
            if not same_run:
                self._turn_epoch += 1
                self._turn_id = normalized
            # Reopening belongs here rather than in a separate entry point: a
            # persistent teammate reuses one run id for every turn, so the
            # NEXT_TURN phase left behind by the previous turn's committed
            # answer would otherwise latch ``mailbox_deliverable`` False for the
            # rest of the process and starve the teammate permanently.  This is
            # the same invariant Codex gets from building a fresh per-turn
            # TurnState whose phase defaults to CurrentTurn.
            self._phase = MailboxDeliveryPhase.CURRENT_TURN
            return self._turn_epoch

    def defer_mailbox_to_next_turn(self, turn_id: str = "") -> bool:
        """Atomically close current-turn mailbox delivery before final output."""
        normalized = str(turn_id or "").strip()
        with self._lock:
            if normalized and self._turn_id and normalized != self._turn_id:
                return False
            if self._phase is not MailboxDeliveryPhase.CURRENT_TURN:
                return False
            self._phase = MailboxDeliveryPhase.NEXT_TURN
            return True

    def mailbox_deliverable(self, turn_id: str = "") -> bool:
        normalized = str(turn_id or "").strip()
        with self._lock:
            return bool(
                self._phase is MailboxDeliveryPhase.CURRENT_TURN
                and (not normalized or not self._turn_id or normalized == self._turn_id)
            )

    def phase_snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "turn_id": self._turn_id,
                "turn_epoch": self._turn_epoch,
                "mailbox_phase": self._phase.value,
                "pending_steer_count": len(self._steering),
            }

    def enqueue_command(
        self,
        command: Any,
        *,
        mode: TurnInputMode = "steer",
        target_message_id: str = "",
    ) -> TurnInput | None:
        waiters: tuple[tuple[asyncio.AbstractEventLoop, asyncio.Event], ...] = ()
        with self._lock:
            if self._phase is not MailboxDeliveryPhase.CURRENT_TURN:
                return None
            item = TurnInput.from_command(
                command,
                mode=mode,
                target_message_id=target_message_id,
            )
            if not item.content.strip() and not item.attachments:
                return None
            self._steering.append(item)
            self._activity_seq += 1
            waiters = tuple(self._activity_waiters)
        self._notify_activity_waiters(waiters)
        return item

    def pop_steer(self) -> TurnInput | None:
        with self._lock:
            if self._phase is not MailboxDeliveryPhase.CURRENT_TURN or not self._steering:
                return None
            return self._steering.popleft()

    def pending_count(self) -> int:
        with self._lock:
            return len(self._steering)

    def snapshot(self) -> tuple[TurnInput, ...]:
        """Return the current FIFO contents without consuming the queue.

        Session restore uses this canonical view so a promoted steer remains
        visible across a renderer reload before the agent reaches its next safe
        consumption boundary.
        """
        with self._lock:
            return tuple(self._steering)

    def seal_and_drain_commands(self) -> list[Any]:
        """Close the epoch and return unconsumed commands in FIFO order."""
        with self._lock:
            self._phase = MailboxDeliveryPhase.SEALED
            commands = [item.original_command for item in self._steering]
            self._steering.clear()
            return commands
