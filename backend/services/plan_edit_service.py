from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from backend.agent.message import AgentEvent


TASK_STATUSES = {"pending", "in_progress", "completed", "blocked"}
PLAN_ACTIONS = {"accept", "reject"}
PLAN_STEP_STATUSES = {"pending", "running", "done", "skipped", "failed"}

PLAN_REJECTED_MESSAGE = "Plan rejected. Ask the agent for a revised plan with the desired changes."


@dataclass(frozen=True)
class PlanEditResult:
    plan_id: str
    action: str
    event: AgentEvent
    rejection_message: str


def build_task_update_event(data: dict[str, Any], *, conversation_id: str = "") -> AgentEvent:
    todo_id = str(data.get("todo_id", "")).strip()
    status = str(data.get("status", "")).strip()
    if not todo_id or status not in TASK_STATUSES:
        raise ValueError("task.edit requires todo_id + valid status")

    event = AgentEvent.task_update(todo_id=todo_id, status=status, content=str(data.get("content", "")))
    clean_conversation_id = str(conversation_id or "").strip()
    if clean_conversation_id:
        event.data["conversation_id"] = clean_conversation_id
    return event


def normalize_plan_steps(raw_steps: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_steps, list):
        return []

    steps: list[dict[str, Any]] = []
    for index, raw_step in enumerate(raw_steps):
        if not isinstance(raw_step, dict):
            continue
        title = str(raw_step.get("title") or raw_step.get("step") or "").strip()
        if not title:
            continue
        status = str(raw_step.get("status") or "pending").strip()
        steps.append({
            "id": str(raw_step.get("id") or f"step-{index}"),
            "title": title,
            "status": status if status in PLAN_STEP_STATUSES else "pending",
            **({"detail": str(raw_step.get("detail"))} if raw_step.get("detail") else {}),
        })
    return steps


def build_plan_edit_result(data: dict[str, Any], *, conversation_id: str = "") -> PlanEditResult:
    plan_id = str(data.get("plan_id") or data.get("planId") or "plan").strip() or "plan"
    action = str(data.get("action") or "").strip().lower()
    if action not in PLAN_ACTIONS:
        raise ValueError("plan.edit requires action 'accept' or 'reject'")

    steps = normalize_plan_steps(data.get("steps"))
    current_step = int(data.get("current_step") or data.get("currentStep") or 0)
    status = "accepted" if action == "accept" else "cancelled"
    event = AgentEvent.plan_updated(
        plan_id=plan_id,
        steps=steps,
        status=status,
        current_step=current_step,
        explanation="Plan accepted by user." if action == "accept" else "Plan rejected by user.",
    )
    clean_conversation_id = str(conversation_id or "").strip()
    if clean_conversation_id:
        event.data["conversation_id"] = clean_conversation_id

    return PlanEditResult(
        plan_id=plan_id,
        action=action,
        event=event,
        rejection_message=PLAN_REJECTED_MESSAGE,
    )
