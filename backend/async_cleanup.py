"""Cleanup primitives for user-facing cancellation paths."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from typing import Any

logger = logging.getLogger(__name__)
CANCELLATION_DRAIN_TIMEOUT_SECONDS = 5.0


def _consume_task_result(task: asyncio.Task[Any]) -> None:
    try:
        task.result()
    except (asyncio.CancelledError, Exception):
        pass


async def cancel_and_drain(
    tasks: Iterable[asyncio.Task[Any]],
    *,
    timeout: float | None,
    label: str,
) -> set[asyncio.Task[Any]]:
    """Cancel tasks and wait for cleanup behind a hard lifecycle bound."""
    current = asyncio.current_task()
    pending = {task for task in tasks if task is not current and not task.done()}
    for task in pending:
        # Cancellation owners commonly signal the task before entering the
        # bounded drain.  Calling ``cancel()`` again while the task is already
        # handling its first ``CancelledError`` can interrupt its terminal
        # commit/finally block and leave the user-visible run without DONE.
        cancelling = getattr(task, "cancelling", None)
        if callable(cancelling) and cancelling() > 0:
            continue
        task.cancel()
    if not pending:
        return set()
    effective_timeout = (
        CANCELLATION_DRAIN_TIMEOUT_SECONDS
        if timeout is None
        else max(0.0, timeout)
    )
    done, still_pending = await asyncio.wait(pending, timeout=effective_timeout)
    for task in done:
        _consume_task_result(task)
    for task in still_pending:
        task.add_done_callback(_consume_task_result)
    if still_pending:
        logger.warning("Timed out draining %s (%d task(s) still pending)", label, len(still_pending))
    return still_pending


async def await_with_deadline(awaitable: Any, *, timeout: float, label: str) -> bool:
    """Wait for cleanup without permitting cancellation-resistant code to block."""
    task = asyncio.ensure_future(awaitable)
    done, pending = await asyncio.wait({task}, timeout=max(0.0, timeout))
    if done:
        try:
            task.result()
        except asyncio.CancelledError:
            return False
        except Exception:
            logger.warning("Failed while awaiting %s", label, exc_info=True)
            return False
        return True
    task.cancel()
    task.add_done_callback(_consume_task_result)
    logger.warning("Timed out awaiting %s", label)
    return False
