from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class SchedulerServiceError(ValueError):
    """User-recoverable scheduler operation failure."""


@dataclass
class SchedulerOperationResult:
    tasks: list[dict[str, Any]]


def get_scheduler_from_bootstrap() -> Any:
    from backend.api import _state as api_state

    bootstrap = api_state.bootstrap
    if bootstrap and getattr(bootstrap, "task_scheduler", None):
        return bootstrap.task_scheduler
    from backend.tasks.scheduler import get_global_scheduler

    return get_global_scheduler()


def list_scheduled_tasks(scheduler: Any) -> SchedulerOperationResult:
    return SchedulerOperationResult(tasks=list(scheduler.list_tasks()))


def add_scheduled_task(scheduler: Any, data: dict[str, Any]) -> SchedulerOperationResult:
    name = _required_field(data.get("name"), "Task name is required")
    prompt = _required_field(data.get("prompt"), "Task prompt is required")
    schedule = _required_field(data.get("schedule"), "Task schedule is required")
    permission_mode = str(data.get("permission_mode", "auto_approve"))
    scheduler.add_task(name=name, prompt=prompt, schedule=schedule, permission_mode=permission_mode)
    return list_scheduled_tasks(scheduler)


def remove_scheduled_task(scheduler: Any, data: dict[str, Any]) -> SchedulerOperationResult:
    task_id = _task_id_from_payload(data)
    if not scheduler.remove_task(task_id):
        raise SchedulerServiceError(f"Task '{task_id}' not found")
    return list_scheduled_tasks(scheduler)


def toggle_scheduled_task(scheduler: Any, data: dict[str, Any]) -> SchedulerOperationResult:
    task_id = _task_id_from_payload(data)
    if not scheduler.toggle_task(task_id, bool(data.get("enabled", True))):
        raise SchedulerServiceError(f"Task '{task_id}' not found")
    return list_scheduled_tasks(scheduler)


def _task_id_from_payload(data: dict[str, Any]) -> str:
    return _required_field(data.get("task_id") or data.get("id"), "Task ID is required")


def _required_field(value: Any, message: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise SchedulerServiceError(message)
    return text
