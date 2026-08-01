from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


class SchedulerServiceError(ValueError):
    """User-recoverable scheduler operation failure."""


@dataclass
class SchedulerOperationResult:
    tasks: list[dict[str, Any]]
    runs: list[dict[str, Any]]


def get_scheduler_from_bootstrap() -> Any:
    from backend.api import _state as api_state

    bootstrap = api_state.bootstrap
    if bootstrap and getattr(bootstrap, "task_scheduler", None):
        return bootstrap.task_scheduler
    from backend.tasks.scheduler import get_global_scheduler

    return get_global_scheduler()


def list_scheduled_tasks(scheduler: Any, *, workspace_root: str | None = None) -> SchedulerOperationResult:
    return SchedulerOperationResult(
        tasks=list(scheduler.list_tasks(workspace_root=workspace_root)),
        runs=list(scheduler.list_runs(workspace_root=workspace_root)),
    )


def add_scheduled_task(scheduler: Any, data: dict[str, Any], *, workspace_root: str | None) -> SchedulerOperationResult:
    from backend.tasks.scheduler import is_valid_timezone, next_run_after

    name = _required_field(data.get("name"), "Task name is required")
    prompt = _required_field(data.get("prompt"), "Task prompt is required")
    schedule = _required_field(data.get("schedule"), "Task schedule is required")
    permission_mode = str(data.get("permission_mode", "confirm"))
    timezone = str(data.get("timezone") or "UTC").strip() or "UTC"
    isolation = str(data.get("isolation") or "worktree").strip().lower()
    if not is_valid_timezone(timezone):
        raise SchedulerServiceError(f"Unknown timezone '{timezone}'")
    if next_run_after(schedule, datetime.now(UTC), timezone=timezone) is None:
        raise SchedulerServiceError("Task schedule must be a valid 5-field cron expression")
    if isolation not in {"worktree", "workspace"}:
        raise SchedulerServiceError("Task isolation must be 'worktree' or 'workspace'")
    # ``None`` is reserved for embedded/legacy API callers that do not expose
    # a workspace accessor. A real WebSocket session passes an empty string
    # when no folder is open and is rejected below.
    if workspace_root == "":
        raise SchedulerServiceError("Open a workspace before creating a scheduled task")
    scheduler.add_task(
        name=name,
        prompt=prompt,
        schedule=schedule,
        permission_mode=permission_mode,
        workspace_root=workspace_root or "",
        conversation_id=str(data.get("conversation_id") or "").strip(),
        timezone=timezone,
        isolation=isolation,
    )
    return list_scheduled_tasks(scheduler, workspace_root=workspace_root)


def remove_scheduled_task(scheduler: Any, data: dict[str, Any], *, workspace_root: str | None = None) -> SchedulerOperationResult:
    task_id = _task_id_from_payload(data)
    if not scheduler.remove_task(task_id, workspace_root=workspace_root):
        raise SchedulerServiceError(f"Task '{task_id}' not found")
    return list_scheduled_tasks(scheduler, workspace_root=workspace_root)


def toggle_scheduled_task(scheduler: Any, data: dict[str, Any], *, workspace_root: str | None = None) -> SchedulerOperationResult:
    task_id = _task_id_from_payload(data)
    if not scheduler.toggle_task(task_id, bool(data.get("enabled", True)), workspace_root=workspace_root):
        raise SchedulerServiceError(f"Task '{task_id}' not found")
    return list_scheduled_tasks(scheduler, workspace_root=workspace_root)


def run_scheduled_task_now(scheduler: Any, data: dict[str, Any], *, workspace_root: str | None = None) -> SchedulerOperationResult:
    task_id = _task_id_from_payload(data)
    run = scheduler.run_now(task_id, workspace_root=workspace_root)
    if run is None:
        raise SchedulerServiceError(f"Task '{task_id}' not found")
    return list_scheduled_tasks(scheduler, workspace_root=workspace_root)


def retry_scheduled_task_run(scheduler: Any, data: dict[str, Any], *, workspace_root: str | None = None) -> SchedulerOperationResult:
    run_id = _required_field(data.get("run_id") or data.get("id"), "Run ID is required")
    run = scheduler.retry_run(run_id, workspace_root=workspace_root)
    if run is None:
        raise SchedulerServiceError(f"Scheduled run '{run_id}' not found")
    return list_scheduled_tasks(scheduler, workspace_root=workspace_root)


def cancel_scheduled_task_run(scheduler: Any, data: dict[str, Any], *, workspace_root: str | None = None) -> SchedulerOperationResult:
    run_id = _required_field(data.get("run_id") or data.get("id"), "Run ID is required")
    if not scheduler.cancel_run(run_id, workspace_root=workspace_root):
        raise SchedulerServiceError(f"Scheduled run '{run_id}' is not running")
    return list_scheduled_tasks(scheduler, workspace_root=workspace_root)


def _task_id_from_payload(data: dict[str, Any]) -> str:
    return _required_field(data.get("task_id") or data.get("id"), "Task ID is required")


def _required_field(value: Any, message: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise SchedulerServiceError(message)
    return text
