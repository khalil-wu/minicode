from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class CommandOutcome:
    command: str
    message: str
    level: str = "info"
    data: dict[str, Any] | None = None


def stop_task(product_manager: Any | None, task_id: str) -> CommandOutcome:
    clean_task_id = str(task_id or "").strip()
    if not clean_task_id:
        return CommandOutcome("task.stop", "Task ID is required", level="error")
    if product_manager is None:
        return CommandOutcome("task.stop", "Task manager not available", level="error")
    snapshot = product_manager.cancel_task(clean_task_id)
    if snapshot is None:
        return CommandOutcome(
            "task.stop",
            f"Task '{clean_task_id}' not found or cannot be stopped",
            level="warning",
        )
    return CommandOutcome(
        "task.stop",
        f"Task '{snapshot.label}' stopped",
        level="success",
        data={"task_id": clean_task_id, "task": snapshot.to_dict()},
    )
