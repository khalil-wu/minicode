"""An isolated V8 context with cancellable execution and no host I/O bindings."""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from functools import cache

from backend.async_cleanup import to_thread_cancel_safe


@cache
def _bootstrap() -> str:
    return Path(__file__).with_suffix(".js").read_text(encoding="utf-8")


@cache
def _runtime():
    from py_mini_racer import MiniRacer, init_mini_racer

    init_mini_racer(ignore_duplicate_init=True)
    _bootstrap()
    return MiniRacer


class CodeVM:
    def __init__(self):
        self.context = None
        self.control = None
        self.execution_seconds = 0.0
        self._closed = False

    async def step(self, operation: str = "", payload=None) -> dict:
        if self._closed:
            raise RuntimeError("Code cell has closed")
        if self.context is None:
            runtime = await to_thread_cancel_safe(_runtime)
            # MiniRacer captures this loop for its native callbacks. Only the
            # library/file loading crosses the worker boundary.
            self.context = runtime()
            self.context.set_hard_memory_limit(128 * 1024 * 1024)
        started = time.monotonic()
        async with asyncio.timeout(max(.001, 10 - self.execution_seconds)):
            if self.control is None:
                self.control = (await self.context.eval_cancelable(_bootstrap())).cancelable()
            if operation:
                await self.control(operation, json.dumps(payload, ensure_ascii=False))
            packet = json.loads(await self.control("drain", ""))
        self.execution_seconds += time.monotonic() - started
        # V8 performs microtask checkpoints on its execution thread. A drain
        # request follows that checkpoint; Python never busy-polls JS jobs.
        packet["more_jobs"] = False
        return packet

    def close(self):
        self._closed = True
        self.control = None
        if self.context is not None:
            self.context.close()
            self.context = None
