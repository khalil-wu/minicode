from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Callable
from uuid import uuid4


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class ManagedTask:
    id: str
    kind: str
    status: str = "running"
    created_at: str = field(default_factory=_utc_now_iso)
    updated_at: str = field(default_factory=_utc_now_iso)
    result: Any = None
    error: str | None = None
    task: asyncio.Task[Any] | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in {"completed", "failed", "cancelled"}

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "result": self.result,
            "error": self.error,
        }


class TaskManager:
    """Track long-running runtime tasks with a small, stable API."""

    def __init__(
        self,
        *,
        max_tasks: int = 128,
        terminal_task_ttl_seconds: float | None = 1800.0,
        on_change: Callable[[], None] | None = None,
    ) -> None:
        self._tasks: dict[str, ManagedTask] = {}
        self._max_tasks = max(1, int(max_tasks))
        self._terminal_task_ttl_seconds = (
            float(terminal_task_ttl_seconds)
            if terminal_task_ttl_seconds is not None
            else None
        )
        self._on_change = on_change

    def create(self, kind: str, awaitable: Any, *, timeout: float | None = None) -> ManagedTask:
        self.prune(notify=False)
        _seq = getattr(self, "_seq", 0) + 1
        self._seq = _seq
        task_id = f"task_{_seq:05d}_{uuid4().hex[:12]}"
        # Own the caller's coroutine immediately. Wrapping an unscheduled
        # coroutine in another coroutine leaves a cancellation window where
        # the wrapper can be cancelled before its first ``await``, producing
        # "coroutine was never awaited" and dropping the actual agent run.
        source_task = asyncio.ensure_future(awaitable)
        if timeout is None:
            task = source_task
        else:
            task = asyncio.create_task(
                self._wrap_awaitable(source_task, timeout=timeout)
            )

            def _cancel_source_if_supervisor_stops(finished: asyncio.Task[Any]) -> None:
                if finished.cancelled() and not source_task.done():
                    source_task.cancel()

            task.add_done_callback(_cancel_source_if_supervisor_stops)
        managed = ManagedTask(id=task_id, kind=kind, task=task)
        self._tasks[task_id] = managed
        task.add_done_callback(lambda finished, managed_task=managed: self._finalize(managed_task, finished))
        self._enforce_task_limit()
        self._notify_changed()
        return managed

    def get(self, task_id: str) -> ManagedTask | None:
        return self._tasks.get(task_id)

    def list(self) -> list[ManagedTask]:
        return list(self._tasks.values())

    def summary(self) -> dict[str, int]:
        counts = {
            "total": len(self._tasks),
            "pending": 0,
            "running": 0,
            "completed": 0,
            "failed": 0,
            "cancelled": 0,
        }
        for task in self._tasks.values():
            if task.status in counts:
                counts[task.status] += 1
        return counts

    def prune(self, *, notify: bool = True) -> int:
        removed = self._prune_expired_terminal_tasks()
        removed += self._enforce_task_limit()
        if notify and removed > 0:
            self._notify_changed()
        return removed

    def cancel(self, task_id: str) -> bool:
        managed = self._tasks.get(task_id)
        if managed is None or managed.task is None:
            return False
        if managed.task.done():
            return False
        managed.status = "cancelled"
        managed.updated_at = _utc_now_iso()
        managed.task.cancel()
        self._notify_changed()
        return True

    async def wait(self, task_id: str) -> Any:
        managed = self._tasks.get(task_id)
        if managed is None or managed.task is None:
            raise KeyError(task_id)
        return await managed.task

    async def cancel_all_and_wait(self) -> int:
        """Cancel and drain every live managed task.

        Cancellation without awaiting leaves the wrapped coroutine pending
        until event-loop destruction, where Python can inject ``GeneratorExit``
        into application code. Session/application shutdown uses this method as
        the final ownership boundary for all managed work.
        """
        current = asyncio.current_task()
        pending: list[asyncio.Task[Any]] = []
        for managed in self._tasks.values():
            task = managed.task
            if task is None or task.done() or task is current:
                continue
            managed.status = "cancelled"
            managed.updated_at = _utc_now_iso()
            task.cancel()
            pending.append(task)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
            self._notify_changed()
        return len(pending)

    async def _wrap_awaitable(self, awaitable: Any, *, timeout: float | None) -> Any:
        if timeout is None:
            return await awaitable
        return await asyncio.wait_for(awaitable, timeout=timeout)

    def _finalize(self, managed: ManagedTask, task: asyncio.Task[Any]) -> None:
        managed.updated_at = _utc_now_iso()
        try:
            managed.result = task.result()
            if managed.status != "cancelled":
                managed.status = "completed"
        except asyncio.CancelledError:
            managed.status = "cancelled"
        except Exception as exc:  # pragma: no cover - callback path is exercised via public API
            managed.status = "failed"
            managed.error = str(exc)
        finally:
            self.prune(notify=False)
            self._notify_changed()

    def _notify_changed(self) -> None:
        callback = self._on_change
        if callback is None:
            return
        try:
            callback()
        except Exception:
            # Observability callbacks should never disrupt task lifecycle updates.
            return

    def _prune_expired_terminal_tasks(self) -> int:
        ttl = self._terminal_task_ttl_seconds
        if ttl is None:
            return 0

        now = datetime.now(UTC)
        removed = 0
        to_delete: list[str] = []
        for task_id, managed in self._tasks.items():
            if not managed.is_terminal:
                continue
            try:
                updated = datetime.fromisoformat(managed.updated_at)
            except ValueError:
                to_delete.append(task_id)
                continue
            if (now - updated).total_seconds() >= ttl:
                to_delete.append(task_id)

        for task_id in to_delete:
            if task_id in self._tasks:
                del self._tasks[task_id]
                removed += 1
        return removed

    def _enforce_task_limit(self) -> int:
        if len(self._tasks) <= self._max_tasks:
            return 0

        removed = 0
        terminal_tasks = [
            managed
            for managed in self._tasks.values()
            if managed.is_terminal
        ]
        terminal_tasks.sort(key=lambda item: (item.updated_at, item.id))

        for managed in terminal_tasks:
            if len(self._tasks) <= self._max_tasks:
                break
            self._tasks.pop(managed.id, None)
            removed += 1

        return removed
