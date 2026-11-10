"""Session-owned state for the durable UI agent-state projection."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import time
from typing import Any


@dataclass(slots=True)
class UiAgentStateStore:
    """Own the cache, pending writes, debounce tasks, and terminal fences.

    These values form one projection state machine. Keeping them together
    prevents a cache entry from outliving its pending write or terminal fence.
    """

    cache: dict[str, tuple[int, dict[str, Any]]] = field(default_factory=dict)
    pending: dict[str, tuple[int, dict[str, Any]]] = field(default_factory=dict)
    tasks: dict[str, asyncio.Task[Any]] = field(default_factory=dict)
    terminal_fences: dict[str, str] = field(default_factory=dict)
    startup_reset_fields: dict[str, set[str]] = field(default_factory=dict)
    revisions: dict[str, int] = field(default_factory=dict)

    def next_revision(self, conversation_id: str, previous: int) -> int:
        # Reserve before yielding to persistence. An event received during the
        # write must follow that revision even if the wall clock has not moved.
        revision = max(previous + 1, self.revisions.get(conversation_id, 0) + 1, time.time_ns())
        self.revisions[conversation_id] = revision
        return revision

