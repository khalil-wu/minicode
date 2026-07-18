"""ScheduleCron tool — register a recurring cron-scheduled task.

Thin wrapper over the existing TaskScheduler (backend/tasks/scheduler.py),
exposing cc-style ScheduleCron to the model.
"""
from __future__ import annotations

from typing import Any

from backend.permissions.context import ToolExecutionContext
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema


class ScheduleCronTool(BaseTool):
    """Register a recurring prompt task on a cron schedule."""

    name = "schedule_cron"
    mutates_workspace = False
    read_only = False
    permission = PermissionLevel.CONFIRM
    description = (
        "Schedule a recurring background task on a cron expression. The task fires the given prompt "
        "at the schedule (min hour day-of-month month day-of-week, e.g. '0 9 * * 1-5' = 9am weekdays). "
        "Use for periodic checks/devops. Returns the scheduled task; the scheduler must be running."
    )

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Short task name."},
                    "prompt": {"type": "string", "description": "Prompt to run on each fire."},
                    "cron": {"type": "string", "description": "5-field cron expression, e.g. '0 9 * * 1-5'."},
                    "permission_mode": {"type": "string", "enum": ["auto_approve", "ask", "bypass"], "description": "Permission mode for fired runs. Defaults to auto_approve."},
                },
                "required": ["name", "prompt", "cron"],
            },
        )

    async def execute(self, args: dict[str, Any], context: ToolExecutionContext | None = None) -> ToolResult:
        name = str(args.get("name") or "").strip()
        prompt = str(args.get("prompt") or "").strip()
        cron = str(args.get("cron") or "").strip()
        if not name or not prompt or not cron:
            return self._error_result("name, prompt, and cron are all required")
        permission_mode = str(args.get("permission_mode") or "auto_approve").strip() or "auto_approve"

        # Validate the cron expression via the scheduler's parser before registering.
        from backend.tasks.scheduler import _parse_cron_expression, get_global_scheduler
        if _parse_cron_expression(cron) is None:
            return self._error_result(f"Invalid cron expression: {cron}")

        try:
            scheduler = get_global_scheduler()
            task = scheduler.add_task(name=name, prompt=prompt, schedule=cron, permission_mode=permission_mode)
        except Exception as exc:
            return self._error_result(f"Failed to schedule task: {exc}")

        return self._success_result(
            content=f"Scheduled '{name}' on cron '{cron}' (mode={permission_mode}).",
            display_summary=f"Scheduled {name}",
        )
