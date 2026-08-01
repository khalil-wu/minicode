from __future__ import annotations

import asyncio
import os
from typing import Any

from backend.agent.message import AgentEvent
from backend.agent.runtime import default_runtime
from backend.permissions.context import ToolExecutionContext
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema
from backend.tools.subagent_result import full_subagent_result
from backend.tools.subagent_runtime import runtime_from_context


def _authorized_subagent(runtime: Any, subagent_id: str, context: ToolExecutionContext | None) -> bool:
    record = runtime.get_subagent(subagent_id)
    get_task_metadata = getattr(runtime, "get_subagent_task_metadata", None)
    task_metadata = get_task_metadata(subagent_id) if callable(get_task_metadata) else None
    if record is None and not isinstance(task_metadata, dict):
        return False
    metadata = context.metadata if context and isinstance(context.metadata, dict) else {}
    parent_run_id = str(metadata.get("run_id") or "").strip()
    # Tool callers created outside an active agent turn (maintenance/unit tests)
    # have no parent identity. Preserve read-only compatibility there ONLY under
    # a test harness; in production an absent parent identity must not be able to
    # control (or read) arbitrary subagents, so we deny by default.
    if not parent_run_id:
        return bool(os.environ.get("PYTEST_CURRENT_TEST"))
    record_parent_run_id = (
        str(getattr(record, "parent_run_id", "") or "")
        if record is not None
        else str(task_metadata.get("parent_run_id") or "")
    )
    if record_parent_run_id != parent_run_id:
        return False
    parent = runtime.get_run(parent_run_id) if hasattr(runtime, "get_run") else None
    conversation_id = str(getattr(context, "conversation_id", "") or "").strip()
    if conversation_id and parent is not None and str(getattr(parent, "conversation_id", "") or "") != conversation_id:
        return False
    return True


class TaskStopTool(BaseTool):
    """Cancel a background subagent started by TaskTool."""

    name = "task_stop"
    should_defer = False
    result_kind = "subagent"
    activity_kind = "genericTool"
    display_label = "Stop task"
    description = "Cancel a running background subagent by id."
    permission = PermissionLevel.AUTO

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "subagent_id": {
                        "type": "string",
                        "description": "The id returned by task(..., run_in_background=true).",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Optional concise reason for cancellation.",
                    },
                },
                "required": ["subagent_id"],
            },
        )

    async def execute(
        self,
        args: dict[str, Any],
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        subagent_id = str(args.get("subagent_id") or "").strip()
        if not subagent_id:
            return ToolResult(
                content="Missing subagent_id argument.",
                is_error=True,
                status="blocked",
                display_summary="Missing subagent id",
                result_kind="subagent",
            )

        runtime = runtime_from_context(context) or default_runtime()
        if not _authorized_subagent(runtime, subagent_id, context):
            return ToolResult(content="Subagent is not owned by the current task.", is_error=True, status="forbidden", result_kind="subagent")
        status = runtime.cancel_subagent_task(subagent_id)
        emit_event = context.emit_event if context else None

        if status == "cancelled":
            if emit_event is not None:
                await emit_event(
                    "subagent.progress",
                    AgentEvent.subagent_progress(
                        subagent_id=subagent_id,
                        detail="cancelling",
                        activity_kind="lifecycle",
                        activity_summary="正在停止子任务",
                        user_visible=True,
                    ).data,
                )
            return ToolResult(
                content=f"Cancellation requested for background subagent {subagent_id}.",
                display_summary=f"Cancelling {subagent_id}",
                result_kind="subagent",
                status="cancelled",
            )

        if status == "done":
            return ToolResult(
                content=f"Background subagent {subagent_id} has already finished.",
                display_summary=f"{subagent_id} already finished",
                result_kind="subagent",
                status="completed",
            )

        record = runtime.get_subagent(subagent_id)
        if record is not None and record.status != "running":
            return ToolResult(
                content=f"Subagent {subagent_id} is already {record.status}.",
                display_summary=f"{subagent_id} {record.status}",
                result_kind="subagent",
                status=record.status,
            )

        return ToolResult(
            content=f"No running background subagent found for {subagent_id}.",
            is_error=True,
            status="not_found",
            display_summary=f"{subagent_id} not found",
            result_kind="subagent",
        )


class TaskStatusTool(BaseTool):
    """Inspect or collect a background subagent result."""

    name = "task_status"
    should_defer = False
    result_kind = "subagent"
    activity_kind = "genericTool"
    display_label = "Task status"
    description = (
        "List background subagents when no id is provided, inspect one subagent with subagent_id, "
        "or collect several concurrently with subagent_ids. Set wait_seconds to wait briefly; batch waits "
        "return as soon as any selected subagent finishes, then include current status for the whole batch."
    )
    permission = PermissionLevel.AUTO
    read_only = True
    mutates_workspace = False
    max_result_chars = None

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "subagent_id": {
                        "type": "string",
                        "description": "Optional id returned by task(..., run_in_background=true). Omit to list background subagents.",
                    },
                    "subagent_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": 8,
                        "description": "Optional batch of up to 8 ids. Prefer this after parallel delegation so all workers are waited for and collected in one call.",
                    },
                    "wait_seconds": {
                        "type": "number",
                        "default": 0,
                        "maximum": 600,
                        "description": "Wait up to this many seconds for the selected subagent to finish; max 600.",
                    },
                    "include_completed": {
                        "type": "boolean",
                        "default": True,
                        "description": "When listing, include completed, failed, and cancelled subagents.",
                    },
                    "limit": {
                        "type": "integer",
                        "default": 20,
                        "description": "Maximum subagents returned when listing; max 100.",
                    },
                    "include_result": {
                        "type": "boolean",
                        "default": True,
                        "description": "Include the retained result content if the subagent has finished.",
                    },
                    "consume": {
                        "type": "boolean",
                        "default": False,
                        "description": "After returning an available result, release the retained result cache.",
                    },
                },
            },
        )

    async def execute(
        self,
        args: dict[str, Any],
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        raw_subagent_ids = args.get("subagent_ids")
        if isinstance(raw_subagent_ids, list):
            subagent_ids = list(dict.fromkeys(
                str(value or "").strip()
                for value in raw_subagent_ids
                if str(value or "").strip()
            ))
            if subagent_ids:
                if len(subagent_ids) > 8:
                    return ToolResult(
                        content=f"Too many subagent ids ({len(subagent_ids)}). Max is 8.",
                        is_error=True,
                        status="blocked",
                        result_kind="subagent",
                    )
                runtime = runtime_from_context(context) or default_runtime()
                unauthorized = [
                    subagent_id
                    for subagent_id in subagent_ids
                    if not _authorized_subagent(runtime, subagent_id, context)
                ]
                if unauthorized:
                    return ToolResult(
                        content="One or more subagents are not owned by the current task.",
                        is_error=True,
                        status="forbidden",
                        result_kind="subagent",
                    )
                child_args = dict(args)
                child_args.pop("subagent_ids", None)
                try:
                    wait_seconds = max(0.0, min(float(child_args.pop("wait_seconds", 0) or 0), 600.0))
                except (TypeError, ValueError):
                    wait_seconds = 0.0
                if wait_seconds:
                    wait_for_any = getattr(runtime, "wait_for_any_subagent", None)
                    if callable(wait_for_any):
                        await wait_for_any(subagent_ids, wait_seconds)
                results = await asyncio.gather(*(
                    self.execute({**child_args, "subagent_id": subagent_id}, context=context)
                    for subagent_id in subagent_ids
                ))
                statuses = {str(result.status or "") for result in results}
                overall_status = (
                    "running" if statuses & {"pending", "running"}
                    else "blocked" if statuses & {"blocked", "failed", "error", "not_found"}
                    else "completed"
                )
                status_counts: dict[str, int] = {}
                for result in results:
                    label = str(result.status or "unknown")
                    status_counts[label] = status_counts.get(label, 0) + 1
                if len(status_counts) == 1:
                    display_summary = f"{len(subagent_ids)} delegated task(s): {overall_status}"
                else:
                    ordered = [
                        f"{count} {label}"
                        for label, count in status_counts.items()
                        if count
                    ]
                    display_summary = (
                        f"{len(subagent_ids)} delegated task(s): "
                        + ", ".join(ordered)
                    )
                return ToolResult(
                    content="\n\n".join(
                        f"### {subagent_id}\n{result.content}"
                        for subagent_id, result in zip(subagent_ids, results)
                    ) + (
                        "\n\nOne or more subagents are still running."
                        if overall_status == "running"
                        else ""
                    ),
                    is_error=any(result.is_error for result in results),
                    display_summary=display_summary,
                    result_kind="subagent",
                    status=overall_status,
                )

        subagent_id = str(args.get("subagent_id") or "").strip()
        if not subagent_id:
            return self._list_subagents(args, context)

        include_result = bool(args.get("include_result", True))
        consume = bool(args.get("consume", False))
        runtime = runtime_from_context(context) or default_runtime()
        if not _authorized_subagent(runtime, subagent_id, context):
            return ToolResult(content="Subagent is not owned by the current task.", is_error=True, status="forbidden", result_kind="subagent")
        try:
            wait_seconds = max(0.0, min(float(args.get("wait_seconds") or 0), 600.0))
        except (TypeError, ValueError):
            wait_seconds = 0.0
        if wait_seconds:
            wait_for_subagent = getattr(runtime, "wait_for_subagent", None)
            if callable(wait_for_subagent):
                await wait_for_subagent(subagent_id, wait_seconds)
        snapshot = runtime.get_subagent_snapshot(subagent_id, include_result=include_result)
        if snapshot is None:
            return ToolResult(
                content=f"No subagent found for {subagent_id}.",
                is_error=True,
                status="not_found",
                display_summary=f"{subagent_id} not found",
                result_kind="subagent",
            )

        status = str(snapshot.get("status") or "running")
        task_label = str(snapshot.get("objective") or snapshot.get("prompt_summary") or "Delegated task").strip()
        background_task = str(snapshot.get("background_task") or "")
        result_available = bool(snapshot.get("result_available"))
        lines = [
            f"Subagent {subagent_id} status: {status}.",
        ]
        if background_task:
            lines.append(f"Background task: {background_task}.")
        if snapshot.get("cancel_requested"):
            lines.append("Cancellation has been requested.")
        if snapshot.get("prompt_summary"):
            lines.append(f"Task: {snapshot.get('prompt_summary')}")

        result = snapshot.get("result")
        if include_result and isinstance(result, dict):
            content = str(result.get("content") or "").strip()
            error = str(result.get("error") or "").strip()
            if error:
                lines.append(f"Error: {error}")
            if content:
                lines.append("Result:")
                rendered, omitted = full_subagent_result(content)
                lines.append(rendered)
                artifact_id = str(result.get("artifact_id") or "").strip()
                if artifact_id:
                    lines.append(f"Full result artifact: {artifact_id}.")
                elif omitted:
                    lines.append("The retained tool details contain the full result.")
            token_total = int(result.get("total_tokens") or 0)
            stats = (
                "Stats: "
                f"{int(result.get('iterations') or 0)} iteration(s), "
                f"{int(result.get('tool_call_count') or 0)} tool call(s), "
                f"{int(result.get('duration_ms') or 0)}ms"
            )
            if token_total:
                stats += (
                    f", {token_total} token(s) "
                    f"(in {int(result.get('input_tokens') or 0)}, "
                    f"out {int(result.get('output_tokens') or 0)})"
                )
            lines.append(stats + ".")
            if consume and status != "running":
                runtime.forget_subagent_result(subagent_id)
                lines.append("Retained result cache released.")
        elif result_available:
            lines.append("Result is available. Call task_status with include_result=true to collect it.")
        else:
            if status in {"pending", "running"}:
                lines.append("Result is not available yet.")
            else:
                lines.append("No retained result is available.")

        return ToolResult(
            content="\n".join(lines),
            display_summary=f"{task_label}: {status}",
            result_kind="subagent",
            status=status,
        )

    def _list_subagents(
        self,
        args: dict[str, Any],
        context: ToolExecutionContext | None,
    ) -> ToolResult:
        runtime = runtime_from_context(context) or default_runtime()
        metadata = context.metadata if context and isinstance(context.metadata, dict) else {}
        parent_run_id = str(metadata.get("run_id") or "").strip()
        conversation_id = str(getattr(context, "conversation_id", "") or "").strip()
        include_completed = bool(args.get("include_completed", True))
        try:
            limit = max(1, min(int(args.get("limit") or 20), 100))
        except (TypeError, ValueError):
            limit = 20
        items = [
            item
            for item in runtime.list_runs(
                conversation_id=conversation_id,
                include_subagents=True,
            ).get("subagents", [])
            if isinstance(item, dict)
            and bool(item.get("background", False))
            and (not parent_run_id or str(item.get("parent_run_id") or "") == parent_run_id)
            and (include_completed or str(item.get("status") or "running") in {"pending", "running"})
        ]
        items.sort(
            key=lambda item: (
                str(item.get("status") or "running") not in {"pending", "running"},
                -int(item.get("started_at") or 0),
            )
        )
        items = items[:limit]
        if not items:
            return ToolResult(
                content="No background subagents matched.",
                display_summary="No background subagents",
                result_kind="subagent",
                status="completed",
            )
        lines = [f"{len(items)} background subagent(s):"]
        for item in items:
            subagent = str(item.get("subagent_id") or "")
            status = str(item.get("status") or "running")
            task = str(item.get("prompt_summary") or item.get("objective") or "").strip()
            result_note = " result available" if item.get("result_available") else ""
            lines.append(
                f"- {subagent} [{status}]{result_note}"
                + (f": {task}" if task else "")
            )
        overall = (
            "running"
            if any(str(item.get("status") or "") in {"pending", "running"} for item in items)
            else "completed"
        )
        return ToolResult(
            content="\n".join(lines),
            display_summary=f"{len(items)} background subagent(s)",
            result_kind="subagent",
            status=overall,
        )
