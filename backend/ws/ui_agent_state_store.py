"""Session-owned state for the durable UI agent-state projection."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
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

