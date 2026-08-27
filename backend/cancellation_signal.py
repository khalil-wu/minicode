"""MiniCode cancellation signal for extension-owned interactions."""

from __future__ import annotations

import asyncio


class CancellationSignal:
    """Read-only cancellation view backed by one owner event."""

    def __init__(self, event: asyncio.Event) -> None:
        self._event = event

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    async def wait(self) -> None:
        await self._event.wait()


__all__ = ["CancellationSignal"]
