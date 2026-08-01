"""Small ordering-preserving batcher for provider reasoning deltas."""

from __future__ import annotations

import time
from dataclasses import dataclass

from backend.agent.message import AgentEvent


_STREAM_KEYS = (
    "source",
    "visibility",
    "is_raw_provider_reasoning",
    "provider_reasoning_type",
    "phase",
    "turn_id",
)


@dataclass
class ReasoningEventBatcher:
    max_chars: int = 512
    max_delay_seconds: float = 0.05
    _pending: AgentEvent | None = None
    _started_at: float = 0.0

    def push(self, event: AgentEvent, *, now: float | None = None) -> list[AgentEvent]:
        current = time.monotonic() if now is None else float(now)
        emitted: list[AgentEvent] = []
        same_stream = bool(
            self._pending is not None
            and all(self._pending.data.get(key) == event.data.get(key) for key in _STREAM_KEYS)
        )
        if self._pending is not None and not same_stream:
            emitted.append(self.flush())
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
