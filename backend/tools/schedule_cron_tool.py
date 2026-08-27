"""Cron tools — create, list, and delete scheduled jobs.

Thin wrapper over the existing TaskScheduler (backend/tasks/scheduler.py).
schedule_cron returns a job ID; schedule_cron_delete cancels a job by that ID.
"""
from __future__ import annotations

from typing import Any

from backend.permissions.context import ToolExecutionContext
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema


class ScheduleCronTool(BaseTool):
    """Register a recurring prompt task on a cron schedule."""

    name = "schedule_cron"
    result_kind = "status"
    activity_kind = "genericTool"
    display_label = "Schedule task"
    mutates_workspace = False
    read_only = False
    permission = PermissionLevel.CONFIRM
    description = (
        "Schedule a recurring background task on a cron expression. The task fires the given prompt "
        "at the schedule (min hour day-of-month month day-of-week, e.g. '0 9 * * 1-5' = 9am weekdays). "
        "Use for periodic checks/devops. Returns a job ID you can pass to schedule_cron_delete."
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
        permission_mode = "confirm"

        # Validate the cron expression via the scheduler's parser before registering.
        from backend.tasks.scheduler import _parse_cron_expression, get_global_scheduler
        if _parse_cron_expression(cron) is None:
            return self._error_result(f"Invalid cron expression: {cron}")

        try:
            scheduler = get_global_scheduler()
            workspace_root = str(getattr(context, "workspace_root", "") or "") if context else ""
            if not workspace_root:
                return self._error_result("Open a workspace before scheduling a recurring task")
            task = scheduler.add_task(
                name=name,
                prompt=prompt,
                schedule=cron,
                permission_mode=permission_mode,
                workspace_root=workspace_root,
                conversation_id=str(getattr(context, "conversation_id", "") or ""),
            )
        except Exception as exc:
            return self._error_result(f"Failed to schedule task: {exc}")

        return self._success_result(
            content=(
                f"Scheduled '{name}' on cron '{cron}' (mode={permission_mode}).\n"
                f"Job ID: {task.id}"
            ),
            display_summary=f"Scheduled {name}",
        )


class ScheduleCronListTool(BaseTool):
    """List scheduled cron jobs (cc: CronList)."""

    name = "schedule_cron_list"
    result_kind = "status"
    activity_kind = "genericTool"
    display_label = "List scheduled tasks"
    mutates_workspace = False
    read_only = True
    permission = PermissionLevel.AUTO
    description = "List all cron jobs scheduled via schedule_cron for the active workspace."

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={"type": "object", "properties": {}, "required": []},
        )

    async def execute(self, args: dict[str, Any], context: ToolExecutionContext | None = None) -> ToolResult:
        try:
            from backend.tasks.scheduler import get_global_scheduler
            scheduler = get_global_scheduler()
            workspace_root = str(getattr(context, "workspace_root", "") or "") if context else ""
            rows = scheduler.list_tasks(workspace_root=workspace_root or None)
        except Exception as exc:
            return self._error_result(f"Failed to list scheduled tasks: {exc}")

        if not rows:
            return self._success_result(
                content="No scheduled jobs found for this workspace.",
                display_summary="No jobs",
            )

        lines = []
        for row in rows:
            status = "enabled" if row.get("enabled", True) else "disabled"
            next_run = row.get("next_run_at") or "n/a"
            lines.append(
                f"- {row.get('id')}: name={row.get('name')!r} cron={row.get('schedule')!r} "
                f"status={status} next_run={next_run}"
            )
        return self._success_result(
            content="\n".join(lines),
            display_summary=f"{len(rows)} scheduled job(s)",
        )


class ScheduleCronDeleteTool(BaseTool):
    """Cancel a scheduled cron job by ID (cc: CronDelete)."""

    name = "schedule_cron_delete"
    result_kind = "status"
    activity_kind = "genericTool"
    display_label = "Delete scheduled task"
    mutates_workspace = False
    read_only = False
    permission = PermissionLevel.CONFIRM
    description = (
        "Cancel a cron job previously scheduled with schedule_cron. "
        "Takes the job ID returned by schedule_cron; use schedule_cron_list to look one up."
    )

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "Job ID returned by schedule_cron.",
                    },
                },
                "required": ["job_id"],
            },
        )

    async def execute(self, args: dict[str, Any], context: ToolExecutionContext | None = None) -> ToolResult:
        job_id = str(args.get("job_id") or "").strip()
        if not job_id:
            return self._error_result("job_id is required")

        try:
            from backend.tasks.scheduler import get_global_scheduler
            scheduler = get_global_scheduler()
            workspace_root = str(getattr(context, "workspace_root", "") or "") if context else ""
            removed = scheduler.remove_task(job_id, workspace_root=workspace_root or None)
        except Exception as exc:
            return self._error_result(f"Failed to delete job '{job_id}': {exc}")

        if not removed:
            return self._error_result(
                f"Job '{job_id}' not found. Use schedule_cron_list to see scheduled jobs."
            )
        return self._success_result(
            content=f"Cancelled scheduled job '{job_id}'.",
            display_summary=f"Cancelled {job_id}",
        )
