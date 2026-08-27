"""Cleanup primitives for user-facing cancellation paths."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)
CANCELLATION_DRAIN_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class CleanupReceipt:
    """Durable-shaped evidence for one cancellation/cleanup attempt.

    ``pending`` is intentionally explicit: a caller may publish a terminal
    run outcome after the cleanup deadline, but it must retain ownership and
    expose that cleanup is still outstanding.
    """

    requested: bool
    acknowledged: bool
    completed: bool
    timed_out: bool
    pending: int

    def to_evidence(self, *, resource_kind: str, resource_id: str, reason: str) -> dict[str, Any]:
        return {
            "resource_kind": str(resource_kind or "task"),
            "resource_id": str(resource_id or ""),
            "reason": str(reason or "cleanup"),
            "requested": self.requested,
            "acknowledged": self.acknowledged,
            "completed": self.completed,
            "timed_out": self.timed_out,
            "pending": self.pending,
        }


async def cancel_and_drain_receipt(
    tasks: Iterable[asyncio.Task[Any]],
    *,
    timeout: float | None,
    label: str,
    owner: set[asyncio.Task[Any]] | None = None,
) -> CleanupReceipt:
    """Cancel tasks once and return bounded, machine-readable cleanup evidence."""
    deadline = time.monotonic() + (
        CANCELLATION_DRAIN_TIMEOUT_SECONDS if timeout is None else max(0.0, timeout)
    )
    task_set = {
        task
        for task in tasks
        if task is not asyncio.current_task() and not task.done()
    }
    pending = await cancel_and_drain(task_set, timeout=max(0.0, deadline - time.monotonic()), label=label)
    if owner is not None and pending:
        owner.update(pending)
        for task in pending:
            retain_cleanup_task(task, owner)
    return CleanupReceipt(
        requested=bool(task_set),
        acknowledged=bool(task_set) and len(pending) < len(task_set),
        completed=not pending,
        timed_out=bool(pending),
        pending=len(pending),
    )


def _consume_task_result(task: asyncio.Task[Any]) -> None:
    try:
        task.result()
    except (asyncio.CancelledError, Exception):
        pass


def retain_cleanup_task(task: asyncio.Task[Any], owner: set[asyncio.Task[Any]]) -> None:
    """Keep a cancellation-resistant task owned until it really settles."""
    owner.add(task)

    def settled(completed: asyncio.Task[Any]) -> None:
        owner.discard(completed)
        _consume_task_result(completed)

    task.add_done_callback(settled)


def cancel_and_retire(
    task: asyncio.Task[Any] | None,
    *,
    owner: set[asyncio.Task[Any]],
) -> None:
    """Cancel one task without blocking the user-facing transition.

    Workspace/conversation switches cannot wait for cancellation-resistant
    background initialization, but the session must keep owning the task until
    it really settles.  Retired tasks therefore remain in ``owner`` for
    shutdown accounting, and their terminal result is always consumed so a
    late failure cannot become an unobserved task exception.
    """

    if task is None:
        return
    if task.done():
        _consume_task_result(task)
        owner.discard(task)
        return

    cancelling = getattr(task, "cancelling", None)
    if not callable(cancelling) or cancelling() <= 0:
        task.cancel()
    owner.add(task)

    def settled(completed: asyncio.Task[Any]) -> None:
        owner.discard(completed)
        _consume_task_result(completed)

    task.add_done_callback(settled)


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


async def cancel_and_drain_to_completion(
    tasks: Iterable[asyncio.Task[Any]],
    *,
    timeout: float | None,
    label: str,
) -> set[asyncio.Task[Any]]:
    """Cancel side-effecting tasks behind a hard deadline.

    Ownership is retained by the task callbacks after the deadline. Waiting
    indefinitely here would let one cancellation-resistant child block the
    canonical run terminal transition forever.
    """
    owned = {
        task
        for task in tasks
        if task is not asyncio.current_task() and not task.done()
    }
    receipt = await cancel_and_drain_receipt(owned, timeout=timeout, label=label)
    if receipt.timed_out:
        logger.warning(
            "Cleanup deadline reached for %s; retaining %d task(s) asynchronously",
            label,
            receipt.pending,
        )
    return {
        task
        for task in owned
        if task is not asyncio.current_task() and not task.done()
    }


async def await_with_deadline(
    awaitable: Any,
    *,
    timeout: float,
    label: str,
    owner: set[Any] | None = None,
) -> bool:
    """Wait for cleanup without losing ownership after the deadline.

    A timeout on an async wrapper around ``asyncio.to_thread`` cannot stop the
    worker thread.  Cancelling only the wrapper therefore creates an unowned
    writer that can mutate a replay/checkpoint file after the caller has moved
    on.  Callers that own the side effect pass their lifecycle set here; the
    task is then left running and retained until it really settles.  The old
    cancellation behavior remains for short-lived callers with no owner.
    """
    task = asyncio.ensure_future(awaitable)
    if task.done():
        try:
            task.result()
        except asyncio.CancelledError:
            return False
        except Exception:
            logger.warning("Failed while awaiting %s", label, exc_info=True)
            return False
        return True

    try:
        # Shield keeps an outer command cancellation from accidentally
        # converting this helper into an unowned provider/resource task. The
        # explicit cancellation below is reserved for this helper's own
        # deadline, where retaining the child would violate the cleanup
        # contract.
        await asyncio.wait_for(
            asyncio.shield(task),
            timeout=max(0.0, timeout),
        )
    except asyncio.TimeoutError:
        if owner is not None:
            retain_cleanup_task(task, owner)
        else:
            task.cancel()
            task.add_done_callback(_consume_task_result)
        logger.warning("Timed out awaiting %s", label)
        return False
    except asyncio.CancelledError:
        if owner is not None:
            retain_cleanup_task(task, owner)
        else:
            task.cancel()
            task.add_done_callback(_consume_task_result)
        raise
    except Exception:
        logger.warning("Failed while awaiting %s", label, exc_info=True)
        return False
    return True
