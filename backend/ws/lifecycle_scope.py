"""The concrete command task can hand its short lifecycle prefix to its run."""
from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class _CommandLifecyclePrefix:
    task: asyncio.Task
    stack: AsyncExitStack


_prefix: ContextVar[_CommandLifecyclePrefix | None] = ContextVar("command_lifecycle_prefix", default=None)


@contextmanager
def bind_command_lifecycle_prefix(stack: AsyncExitStack):
    token = _prefix.set(_CommandLifecyclePrefix(asyncio.current_task(), stack))
    try:
        yield
    finally:
        _prefix.reset(token)


async def handoff_command_lifecycle_prefix() -> None:
    owned = _prefix.get()
    if owned is not None and owned.task is asyncio.current_task():
        await owned.stack.aclose()
