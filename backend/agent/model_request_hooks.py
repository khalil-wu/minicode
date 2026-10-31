"""Own observation hooks for each provider sampling attempt."""

from __future__ import annotations

import asyncio
from typing import Any

from backend.hooks.models import HookEvent


class ModelRequestHooks:
    def __init__(self, manager: Any, *, cancel_event: asyncio.Event | None) -> None:
        self.manager = manager if manager and (
            manager.has_hooks(HookEvent.PRE_MODEL_REQUEST) or manager.has_hooks(HookEvent.POST_MODEL_REQUEST)
        ) else None
        self.cancel_event = cancel_event
        self.pending = False
        self.defer_tool_dispatch = bool(self.manager and self.manager.has_synchronous_post_model_request())

    @classmethod
    def for_turn(cls, context_builder: Any, tool_context: Any) -> "ModelRequestHooks":
        manager = getattr(context_builder, "hook_manager", None)
        if manager is not None and tool_context.metadata.get("internal_session_source") == "memory_consolidation":
            manager = manager.for_memory_consolidation()
        return cls(manager, cancel_event=tool_context.cancel_event)

    async def begin(self) -> None:
        if self.manager is None:
            return
        self.pending = True
        await self.manager.run_pre_model_request()

    def _schedule(self, status: str, error: str | None, usage: Any | None) -> asyncio.Task[Any] | None:
        if not self.pending:
            return None
        self.pending = False
        token_usage = None if usage is None else {
            "input_tokens": usage.input_tokens,
            "cached_input_tokens": usage.cache_read_input_tokens,
            "cache_write_input_tokens": usage.cache_creation_input_tokens,
            "output_tokens": usage.output_tokens,
            "reasoning_output_tokens": usage.reasoning_output_tokens,
            "total_tokens": usage.total_tokens,
        }
        return self.manager.schedule_post_model_request(status=status, error=error, token_usage=token_usage)

    async def finish(self, status: str, *, error: str | None = None, usage: Any | None = None,
                     wait: bool = True) -> None:
        task = self._schedule(status, error, usage)
        if task is None or not self.defer_tool_dispatch or not wait:
            return
        cancel_waiter = asyncio.create_task(self.cancel_event.wait()) if self.cancel_event is not None else None
        try:
            await asyncio.wait({task, cancel_waiter} if cancel_waiter is not None else {task},
                               return_when=asyncio.FIRST_COMPLETED)
            if self.cancel_event is not None and self.cancel_event.is_set():
                raise asyncio.CancelledError
        finally:
            if cancel_waiter is not None:
                cancel_waiter.cancel()
                await asyncio.gather(cancel_waiter, return_exceptions=True)

    async def settle(self, status: str, *, error: str | None = None, usage: Any | None = None,
                     reported: bool = True, tool_stream: Any | None = None, wait: bool = True) -> None:
        """Finish the observer for this attempt and release any tool dispatch it deferred."""
        await self.finish(status, error=error, usage=usage if reported else None, wait=wait)
        if tool_stream is not None:
            tool_stream.release_deferred()

    async def settle_exception(self, exc: BaseException, *, completed: bool, usage: Any | None = None,
                               reported: bool = True) -> None:
        cancelled = isinstance(exc, asyncio.CancelledError)
        await self.finish("completed" if completed else "cancelled" if cancelled else "failed",
                          error=None if completed or cancelled else str(exc),
                          usage=usage if reported else None, wait=not cancelled)

    def abandon(self, *, completed: bool, usage: Any | None, reported: bool = True) -> None:
        # A consumer can close the generator without resuming its exception
        # handler. The session still owns the request observer.
        self._schedule("completed" if completed else "cancelled", None, usage if reported else None)
