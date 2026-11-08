"""Thread-owned interaction registrations and the command observation clock."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import time


class ElicitationService:
    def __init__(self):
        self.external_count = 0
        self._registrations = 0
        self._paused_at: float | None = None
        self._paused_seconds = 0.0
        self.changed = asyncio.Event()

    @property
    def paused(self) -> bool:
        return self.external_count > 0 or self._registrations > 0

    def _publish(self, was_paused: bool) -> None:
        if was_paused == self.paused:
            return
        now = time.monotonic()
        if self.paused:
            self._paused_at = now
        else:
            self._paused_seconds += now - self._paused_at
            self._paused_at = None
        previous, self.changed = self.changed, asyncio.Event()
        previous.set()

    @contextmanager
    def register(self):
        was_paused = self.paused
        self._registrations += 1
        self._publish(was_paused)
        try:
            yield
        finally:
            was_paused = self.paused
            self._registrations -= 1
            self._publish(was_paused)

    def increment(self) -> int:
        if self.external_count == (1 << 63) - 1:
            raise OverflowError("out-of-band elicitation count overflowed")
        was_paused = self.paused
        self.external_count += 1
        self._publish(was_paused)
        return self.external_count

    def decrement(self) -> int:
        if self.external_count == 0:
            raise ValueError("out-of-band elicitation count is already zero")
        was_paused = self.paused
        self.external_count -= 1
        self._publish(was_paused)
        return self.external_count

    def reset_external(self) -> None:
        was_paused = self.paused
        self.external_count = 0
        self._publish(was_paused)

    def active_time(self) -> float:
        return (self._paused_at if self._paused_at is not None else time.monotonic()) - self._paused_seconds

    async def wait_any(self, tasks: set, timeout: float | None):
        """Observe completion promptly while charging only unpaused timeout time."""
        deadline = self.active_time() + timeout if timeout is not None else None
        while True:
            done = {task for task in tasks if task.done()}
            if done:
                return done, tasks - done
            remaining = None if deadline is None or self.paused else max(0.0, deadline - self.active_time())
            changed = asyncio.create_task(self.changed.wait())
            try:
                done, _ = await asyncio.wait(tasks | {changed}, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
                observed = done - {changed}
                if observed or not done:
                    return observed, tasks - observed
            finally:
                changed.cancel()
                await asyncio.gather(changed, return_exceptions=True)

    async def wait_until_clear(self, cancel_event: asyncio.Event | None = None) -> None:
        while self.paused:
            if cancel_event is not None and cancel_event.is_set():
                raise asyncio.CancelledError
            changed = asyncio.create_task(self.changed.wait())
            cancelled = asyncio.create_task(cancel_event.wait()) if cancel_event is not None else None
            tasks = {changed, *([cancelled] if cancelled is not None else [])}
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                if cancelled is not None and cancelled in done:
                    raise asyncio.CancelledError
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
