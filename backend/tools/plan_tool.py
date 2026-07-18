"""update_plan tool — model-driven execution plan with live step progression.

Unlike todo_write (the compact task progress checklist), the plan is a larger
user-visible execution plan rendered in the Plan panel. Each call emits a full
`plan_updated` snapshot so the frontend can create or replace the live plan in
one event.
"""

from __future__ import annotations

import json
import inspect
from pathlib import Path
from typing import Any

from backend.agent.message import AgentEvent
from backend.permissions.context import PermissionContext
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema

MAX_PLAN_STEPS = 30

# Model-facing status vocabulary -> frontend PlanStep vocabulary.
_STATUS_MAP = {
    "pending": "pending",
    "in_progress": "running",
    "completed": "done",
}
_VALID_INPUT_STATUSES = set(_STATUS_MAP)


def _context_state_key(context: Any = None) -> str:
    return str(
        getattr(context, "conversation_id", "")
        or getattr(context, "session_id", "")
        or "default"
    )


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


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
        "When not to use: Do not use for routine task tracking, a simple checklist, a single-step fix, or merely "
        "because todo_write is available. Do not mirror the same todo list into both tools.\n\n"
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

        session_id = _context_state_key(context)
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


class ExitPlanModeTool(BaseTool):
    """Submit a draft plan and wait for user approval before implementation."""

    name = "exit_plan_mode"
    mutates_workspace = False
    read_only = False
    always_load = True
    permission = PermissionLevel.AUTO
    description = (
        "Submit the current plan for user approval before leaving plan mode. "
        "Use after read-only investigation when you have concise implementation steps. "
        "This tool does not grant write permission; the user must accept or reject the draft plan."
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
                        "description": "Ordered draft implementation steps for the user to approve.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "step": {"type": "string"},
                                "status": {
                                    "type": "string",
                                    "enum": ["pending", "in_progress", "completed"],
                                    "default": "pending",
                                },
                            },
                            "required": ["step"],
                        },
                    },
                    "explanation": {
                        "type": "string",
                        "description": "Optional concise rationale for the proposed plan.",
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
            return ToolResult(content="exit_plan_mode requires a non-empty plan array.", is_error=True)
        if len(plan) > MAX_PLAN_STEPS:
            return ToolResult(content=f"Plan cannot exceed {MAX_PLAN_STEPS} steps.", is_error=True)

        steps: list[dict[str, str]] = []
        for index, item in enumerate(plan):
            if isinstance(item, str):
                title = item.strip()
                raw_status = "pending"
            elif isinstance(item, dict):
                title = str(item.get("step") or item.get("title") or "").strip()
                raw_status = str(item.get("status") or "pending").strip().lower()
            else:
                return ToolResult(content=f"Plan step {index + 1} must be an object or string.", is_error=True)
            if not title:
                return ToolResult(content=f"Plan step {index + 1} is missing text.", is_error=True)
            if raw_status not in _VALID_INPUT_STATUSES:
                return ToolResult(
                    content="Invalid plan step status; use pending / in_progress / completed.",
                    is_error=True,
                )
            steps.append(
                {
                    "id": f"step-{index}",
                    "title": title,
                    "status": _STATUS_MAP[raw_status],
                }
            )

        if sum(1 for step in steps if step["status"] == "running") > 1:
            return ToolResult(content="At most one plan step can be in_progress.", is_error=True)

        session_id = _context_state_key(context)
        plan_id = f"plan-{session_id}"
        current_step = UpdatePlanTool._current_step(steps)
        explanation = str(args.get("explanation") or "").strip()
        self._save(session_id, plan_id, steps, "draft", current_step)

        emit_event = getattr(context, "emit_event", None) if context else None
        if emit_event is not None:
            await emit_event(
                "plan_updated",
                AgentEvent.plan_updated(
                    plan_id=plan_id,
                    steps=steps,
                    status="draft",
                    current_step=current_step,
                    explanation=explanation,
                ).data,
            )

        return ToolResult(
            content="Draft plan submitted. Now wait for the user to accept or reject it before making changes.",
            result_kind="plan",
            status="draft",
            display_summary="Submitted draft plan",
        )

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


class EnterPlanModeTool(BaseTool):
    """Request a read-only planning turn."""

    name = "enter_plan_mode"
    mutates_workspace = False
    read_only = False
    always_load = True
    permission = PermissionLevel.AUTO
    description = (
        "Enter read-only plan mode for investigation and design. "
        "Use when you need to inspect and propose a plan before making changes. "
        "Do not use this to bypass approval; finish with exit_plan_mode."
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
                    "reason": {
                        "type": "string",
                        "description": "Brief reason for switching to plan mode.",
                    },
                },
            },
        )

    async def execute(self, args: dict[str, Any], context: Any = None) -> ToolResult:
        reason = str(args.get("reason") or "").strip()
        if context is not None:
            current = getattr(context, "permission", None)
            if isinstance(current, PermissionContext) and current.mode != "plan":
                next_permission = PermissionContext(
                    mode="plan",
                    session_overrides=dict(current.session_overrides),
                    tool_deny_rules=list(current.tool_deny_rules),
                    filesystem_constraints=dict(current.filesystem_constraints),
                    workspace_scope=current.workspace_scope,
                    source="enter_plan_mode",
                )
                context.permission = next_permission
                metadata = getattr(context, "metadata", None)
                setter = metadata.get("permission_mode_setter") if isinstance(metadata, dict) else None
                if callable(setter):
                    await _maybe_await(setter("plan", source="enter_plan_mode"))
        return ToolResult(
            content=" ".join(
                part
                for part in (
                    "Plan mode is active.",
                    f"Reason: {reason}." if reason else "",
                    "Continue with read-only discovery and call exit_plan_mode with a concise draft plan when ready.",
                )
                if part
            ),
            result_kind="plan",
            display_summary="Entered plan mode",
        )
