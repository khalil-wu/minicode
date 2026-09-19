"""Turn-owned ordering and admission shared by direct and composed tool calls."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field


@dataclass
class _ToolLease:
    read_only: bool
    acquired: bool = True
    alive: bool = True
    nested: asyncio.Lock = field(default_factory=asyncio.Lock)


class ToolExecutionGate:
    def __init__(self, *, limit: int, initial_completed: int):
        self.limit = limit
        self.initial_completed = initial_completed
        self.inflight: set[str] = set()
        self._condition = asyncio.Condition()
        self._readers = 0
        self._writer = False
        self._waiting_writers = 0
        self._lease = ContextVar("tool_execution_lease", default=None)

    def admit(self, call_id: str, completed: int) -> bool:
        if call_id in self.inflight:
            return True
        if self.limit and completed - self.initial_completed + len(self.inflight) >= self.limit:
            return False
        self.inflight.add(call_id)
        return True

    def complete(self, call_id: str) -> None:
        self.inflight.discard(call_id)

    async def _acquire(self, read_only: bool):
        async with self._condition:
            if read_only:
                await self._condition.wait_for(lambda: not self._writer and not self._waiting_writers)
                self._readers += 1
            else:
                self._waiting_writers += 1
                try:
                    await self._condition.wait_for(lambda: not self._writer and not self._readers)
                    self._writer = True
                finally:
                    self._waiting_writers -= 1
                    self._condition.notify_all()
    async def _release(self, read_only: bool):
        async with self._condition:
            if read_only:
                self._readers -= 1
            else:
                self._writer = False
            self._condition.notify_all()

    @asynccontextmanager
    async def hold(self, *, read_only: bool):
        await self._acquire(read_only)
        lease = _ToolLease(read_only)
        token = self._lease.set(lease)
        try:
            yield
        finally:
            lease.alive = False
            self._lease.reset(token)
            if lease.acquired:
                await self._release(read_only)

    @asynccontextmanager
    async def suspend(self):
        """Let an extension await a nested canonical call, then resume its body.

        Holding the enclosing writer while awaiting another writer deadlocks.
        Nested commands serialize per enclosing call and acquire their own
        normal gate slot; the enclosing body reacquires before it continues.
        """
        lease = self._lease.get()
        if lease is None:
            yield
            return
        async with lease.nested:
            if not lease.alive:
                raise asyncio.CancelledError
            token = self._lease.set(None)
            try:
                await self._release(lease.read_only)
                lease.acquired = False
                try:
                    yield
                finally:
                    if lease.alive and not asyncio.current_task().cancelling():
                        await self._acquire(lease.read_only)
                        lease.acquired = True
            finally:
                self._lease.reset(token)
