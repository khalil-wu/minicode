"""update_plan tool — model-driven execution plan with live step progression.

Unlike todo_write (the compact task progress checklist), the plan is a larger
user-visible execution plan rendered in the Plan panel. Each call emits a full
`plan_updated` snapshot so the frontend can create or replace the live plan in
one event.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from backend.agent.message import AgentEvent
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema

MAX_PLAN_STEPS = 30

# Model-facing status vocabulary -> frontend PlanStep vocabulary.
_STATUS_MAP = {
    "pending": "pending",
    "in_progress": "running",
    "completed": "done",
}
_VALID_INPUT_STATUSES = set(_STATUS_MAP)


class UpdatePlanTool(BaseTool):
    """Create or update the session's visible execution plan."""

    name = "update_plan"
    mutates_workspace = False
    read_only = False  # mutates session plan state
    permission = PermissionLevel.AUTO
    description = (
        "Maintain the larger user-visible execution plan. This is distinct from todo_write: todo_write drives "
        "the compact live checklist/status island, while update_plan is for a visible phase plan.\n\n"
        "When to use: the user explicitly asks for a plan, the task is ambiguous enough that a visible approach "
        "helps alignment, or the work has larger phases that should remain visible while you execute.\n\n"
        "When not to use: routine task tracking, a simple checklist, a single-step fix, or merely because "
        "todo_write is available. Do not mirror the same routine todo list into both tools.\n\n"
        "State rules: every call sends the full ordered plan snapshot; at most one step may be in_progress; "
        "advance status as phases actually progress; mark completed only after the phase is genuinely done. "
        "Optional explanation should describe why the plan changed, not narrate every tool call."
    )

    def __init__(self, workspace_root: Path | None = None) -> None:
        self._workspace_root = workspace_root

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "plan": {
                        "type": "array",
                        "description": (
                            "Full ordered plan snapshot. Use for a larger visible phase plan, not routine todo "
                            "tracking. At most one step may be in_progress."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "step": {"type": "string", "description": "Concise visible phase title."},
                                "status": {
                                    "type": "string",
                                    "enum": ["pending", "in_progress", "completed"],
                                },
                            },
                            "required": ["step", "status"],
                        },
                    },
                    "explanation": {
                        "type": "string",
                        "description": "Optional note explaining the plan or why this update changed it.",
                    },
                    "status": {
                        "type": "string",
                        "enum": ["draft", "accepted", "executing", "completed", "cancelled"],
                        "description": "Optional plan lifecycle state. Omit to infer from step statuses.",
                    },
                },
                "required": ["plan"],
            },
        )

    def _persist_path(self, session_id: str) -> Path | None:
        if not self._workspace_root:
            return None
        return self._workspace_root / ".minicode" / "plans" / f"{session_id}.json"

    async def execute(self, args: dict[str, Any], context: Any = None) -> ToolResult:
        plan = args.get("plan")
        if not isinstance(plan, list) or not plan:
            return ToolResult(content="update_plan 需要非空的 plan 步骤数组。", is_error=True)
        if len(plan) > MAX_PLAN_STEPS:
            return ToolResult(content=f"计划步骤不能超过 {MAX_PLAN_STEPS} 个。", is_error=True)

        steps: list[dict[str, str]] = []
        in_progress_count = 0
        for index, item in enumerate(plan):
            if not isinstance(item, dict):
                return ToolResult(content=f"第 {index + 1} 个步骤必须是对象。", is_error=True)
            title = str(item.get("step", "")).strip()
            raw_status = str(item.get("status", "pending")).strip().lower()
            if not title:
                return ToolResult(content=f"第 {index + 1} 个步骤缺少 step 标题。", is_error=True)
            if raw_status not in _VALID_INPUT_STATUSES:
                return ToolResult(
                    content=f"无效状态 '{raw_status}'，必须是 pending / in_progress / completed。",
                    is_error=True,
                )
            if raw_status == "in_progress":
                in_progress_count += 1
            steps.append({
                "id": f"step-{index}",
                "title": title,
                "status": _STATUS_MAP[raw_status],
            })

        if in_progress_count > 1:
            return ToolResult(content="任意时刻最多只能有一个 in_progress 步骤。", is_error=True)

        session_id = str(getattr(context, "session_id", "") or "default") if context else "default"
        plan_id = f"plan-{session_id}"
        current_step = self._current_step(steps)
        all_done = all(step["status"] == "done" for step in steps)
        raw_plan_status = str(args.get("status", "") or "").strip().lower()
        if raw_plan_status and raw_plan_status not in {"draft", "accepted", "executing", "completed", "cancelled"}:
            return ToolResult(
                content="无效计划状态，必须是 draft / accepted / executing / completed / cancelled。",
                is_error=True,
            )
        if raw_plan_status:
            plan_status = raw_plan_status
        elif all_done:
            plan_status = "completed"
        elif in_progress_count:
            plan_status = "executing"
        else:
            plan_status = "draft"
        explanation = str(args.get("explanation", "") or "").strip()

        self._save(session_id, plan_id, steps, plan_status, current_step)

        emit_event = getattr(context, "emit_event", None) if context else None
        if emit_event is not None:
            await emit_event(
                "plan_updated",
                AgentEvent.plan_updated(
                    plan_id=plan_id,
                    steps=steps,
                    status=plan_status,
                    current_step=current_step,
                    explanation=explanation,
                ).data,
            )

        done = sum(1 for step in steps if step["status"] == "done")
        running = next((step["title"] for step in steps if step["status"] == "running"), "")
        summary = f"计划已更新：{len(steps)} 步，状态 {plan_status}，已完成 {done}。"
        if running:
            summary += f" 进行中：{running}。"
        elif all_done:
            summary += " 全部完成。"
        return ToolResult(content=summary)

    @staticmethod
    def _current_step(steps: list[dict[str, str]]) -> int:
        for index, step in enumerate(steps):
            if step["status"] == "running":
                return index
        for index, step in enumerate(steps):
            if step["status"] == "pending":
                return index
        return len(steps)

    def _save(self, session_id: str, plan_id: str, steps: list[dict[str, str]], status: str, current_step: int) -> None:
        path = self._persist_path(session_id)
        if not path:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {"plan_id": plan_id, "status": status, "current_step": current_step, "steps": steps},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception:
            pass
