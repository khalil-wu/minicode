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


def respond_to_approval(
    approval_manager: Any | None,
    approval_id: str,
    action: str,
    *,
    guidance: Any = None,
) -> CommandOutcome:
    clean_approval_id = str(approval_id or "").strip()
    clean_action = str(action or "").strip().lower()
    if not clean_approval_id:
        return CommandOutcome("approval.respond", "Approval ID is required", level="error")
    if clean_action not in {"approve", "reject"}:
        return CommandOutcome("approval.respond", "Action must be 'approve' or 'reject'", level="error")
    if approval_manager is None:
        return CommandOutcome("approval.respond", "Approval manager not available", level="error")

    approval = approval_manager.get_approval(clean_approval_id)
    if approval is None:
        return CommandOutcome(
            "approval.respond",
            f"Approval '{clean_approval_id}' not found",
            level="warning",
        )

    guidance_value = guidance if isinstance(guidance, str) else None
    if clean_action == "approve":
        approval_manager.resolve_approval(clean_approval_id, "approve")
        return CommandOutcome(
            "approval.respond",
            f"Approved: {approval.title}",
            level="success",
            data={"approval_id": clean_approval_id, "action": "approve"},
        )

    approval_manager.resolve_approval(clean_approval_id, "reject", guidance=guidance_value)
    guidance_text = f" with guidance: {guidance}" if guidance else ""
    return CommandOutcome(
        "approval.respond",
        f"Rejected: {approval.title}{guidance_text}",
        level="success",
        data={"approval_id": clean_approval_id, "action": "reject", "guidance": guidance},
    )
