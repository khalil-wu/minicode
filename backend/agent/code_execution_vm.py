"""An isolated V8 context with cancellable execution and no host I/O bindings."""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from py_mini_racer import MiniRacer

_BOOTSTRAP = Path(__file__).with_suffix(".js").read_text(encoding="utf-8")


class CodeVM:
    def __init__(self):
        self.context = MiniRacer()
        self.context.set_hard_memory_limit(128 * 1024 * 1024)
        self.control = None
        self.execution_seconds = 0.0

    async def step(self, operation: str = "", payload=None) -> dict:
        started = time.monotonic()
        async with asyncio.timeout(max(.001, 10 - self.execution_seconds)):
            if self.control is None:
                self.control = (await self.context.eval_cancelable(_BOOTSTRAP)).cancelable()
            if operation:
                await self.control(operation, json.dumps(payload, ensure_ascii=False))
            packet = json.loads(await self.control("drain", ""))
        self.execution_seconds += time.monotonic() - started
        # V8 performs microtask checkpoints on its execution thread. A drain
        # request follows that checkpoint; Python never busy-polls JS jobs.
        packet["more_jobs"] = False
        return packet

    def close(self):
        self.control = None
        self.context.close()
