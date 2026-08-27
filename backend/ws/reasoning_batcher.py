"""Small ordering-preserving batcher for provider reasoning deltas."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from backend.agent.message import AgentEvent


logger = logging.getLogger(__name__)


_STREAM_KEYS = (
    "conversation_id",
    "message_id",
    "task_id",
    "source",
    "visibility",
    "phase",
    "turn_id",
    "item_id",
    "content_index",
    "lifecycle",
)


@dataclass
class ReasoningEventBatcher:
    max_chars: int = 512
    max_delay_seconds: float = 0.05
    _pending: AgentEvent | None = None
    _started_at: float = 0.0
    _active_stream: tuple[object, ...] | None = None
    _first_event_emitted: bool = False

    @property
    def has_pending(self) -> bool:
        return self._pending is not None

    def push(self, event: AgentEvent, *, now: float | None = None) -> list[AgentEvent]:
        current = time.monotonic() if now is None else float(now)
        emitted: list[AgentEvent] = []
        stream = (
            event.type,
            *(event.data.get(key) for key in _STREAM_KEYS),
        )
        same_stream = self._active_stream == stream
        if self._pending is not None and not same_stream:
            emitted.append(self.flush())
        if not same_stream:
            self._active_stream = stream
            self._first_event_emitted = False

        # Do not hold the first visible reasoning chunk behind the batching
        # deadline. It establishes the live UI item immediately; later chunks
        # for the same item still coalesce by size/frame-friendly deadline.
        if not self._first_event_emitted:
            self._first_event_emitted = True
            emitted.append(event)
            return emitted

        if self._pending is None:
            self._pending = AgentEvent(type=event.type, data=dict(event.data))
            self._started_at = current
        else:
            self._pending.data["content"] = (
                str(self._pending.data.get("content") or "")
                + str(event.data.get("content") or "")
            )
        if (
            len(str(self._pending.data.get("content") or "")) >= max(1, int(self.max_chars))
            or current - self._started_at >= max(0.0, float(self.max_delay_seconds))
        ):
            emitted.append(self.flush())
        return emitted

    def flush(self) -> AgentEvent:
        if self._pending is None:
            raise RuntimeError("reasoning batch is empty")
        event = self._pending
        self._pending = None
        self._started_at = 0.0
        return event

    def flush_if_pending(self) -> AgentEvent | None:
        return self.flush() if self._pending is not None else None


class ReasoningFlushDeadline:
    """One-shot, run-owned deadline for a pending reasoning batch."""

    def __init__(
        self,
        delay_seconds: float,
        callback: Callable[[], Awaitable[None]],
    ) -> None:
        self._delay_seconds = max(0.0, float(delay_seconds))
        self._callback = callback
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    @property
    def armed(self) -> bool:
        return self._task is not None and not self._task.done()

    def arm(self) -> None:
        """Start the deadline once; repeated calls do not extend it."""

        if self._closed or self.armed:
            return
        task = asyncio.create_task(
            self._wait_and_flush(),
            name="reasoning-batch-deadline",
        )
        self._task = task
        task.add_done_callback(self._consume_result)

    async def disarm(self) -> None:
        task = self._task
        if task is None:
            return
        if task is asyncio.current_task():
            return
        self._task = None
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def close(self) -> None:
        self._closed = True
        await self.disarm()

    async def _wait_and_flush(self) -> None:
        current = asyncio.current_task()
        try:
            await asyncio.sleep(self._delay_seconds)
            await self._callback()
        except asyncio.CancelledError:
            raise
        finally:
            if self._task is current:
                self._task = None

    @staticmethod
    def _consume_result(task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:
            logger.exception("Reasoning batch deadline callback failed")
