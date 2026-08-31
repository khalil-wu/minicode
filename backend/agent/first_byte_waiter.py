"""Bounded provider-stream event acquisition."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any

from backend.async_cleanup import cancel_and_drain, cancel_and_drain_receipt


class ProviderStreamFailure(RuntimeError):
    """An exception raised by the provider iterator, not by loop processing."""

    def __init__(self, cause: Exception) -> None:
        super().__init__(str(cause))
        self.cause = cause


async def wait_for_provider_event(
    stream_iter: Any,
    *,
    timeout_seconds: float | None,
    cancel_event: asyncio.Event | None,
    owner: set[asyncio.Task[Any]],
) -> Any:
    """Wait for one provider event with bounded cancellation cleanup."""
    event_task = asyncio.create_task(stream_iter.__anext__())
    cancel_task = (
        asyncio.create_task(cancel_event.wait())
        if cancel_event is not None
        else None
    )
    wait_set: set[asyncio.Task[Any]] = {event_task}
    if cancel_task is not None:
        wait_set.add(cancel_task)
    try:
        done_tasks, _ = await asyncio.wait(
            wait_set,
            timeout=(
                None
                if timeout_seconds is None
                else max(0.0, float(timeout_seconds))
            ),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if cancel_task is not None and cancel_task in done_tasks:
            await cancel_and_drain_receipt(
                [event_task],
                timeout=0.25,
                label="provider stream cancellation",
                owner=owner,
            )
            raise asyncio.CancelledError
        if event_task not in done_tasks:
            await cancel_and_drain_receipt(
                [event_task],
                timeout=0.25,
                label="provider stream timeout",
                owner=owner,
            )
            raise asyncio.TimeoutError
        try:
            return event_task.result()
        except StopAsyncIteration:
            raise
        except Exception as exc:
            raise ProviderStreamFailure(exc) from exc
    finally:
        if cancel_task is not None and not cancel_task.done():
            await cancel_and_drain(
                [cancel_task],
                timeout=0.05,
                label="provider stream cancel watcher",
            )
