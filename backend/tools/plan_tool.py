"""update_plan tool — model-driven execution plan with live step progression.

Unlike todo_write (a private checklist), the plan is a user-visible execution
plan rendered in the Plan panel. Each call emits a full `plan_updated` snapshot
so the frontend can create or replace the live plan in one event.
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
        "维护用户可见的执行计划（区别于 todo_write 的私有清单）。"
        "当任务需要多步骤、跨文件或阶段验证时，先用 update_plan 给出完整步骤，"
        "并在推进时再次调用以更新每个步骤的状态。"
        "参数 plan 是步骤数组，每个步骤含 step（标题）和 status"
        "（pending / in_progress / completed）；任意时刻最多一个 in_progress。"
        "可选 explanation 说明计划或本次更新的原因。"
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
                        "description": "Ordered plan steps.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "step": {"type": "string", "description": "Step title."},
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
                        "description": "Optional note about the plan or this update.",
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
        plan_status = "completed" if all_done else "executing"
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
        summary = f"计划已更新：{len(steps)} 步，已完成 {done}。"
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
