from __future__ import annotations

import asyncio
from typing import Any

from backend.agent.message import AgentEvent
from backend.permissions.context import ToolExecutionContext
from backend.tools.agent_control_plane import (
    AgentControlPlane,
    AgentTarget,
    AgentTreeScope,
    canonical_agent_operation,
)
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema
from backend.tools.subagent_result import full_subagent_result
from backend.tools.subagent_runtime import require_runtime_from_context


def _authorized_subagent(
    runtime: Any,
    subagent_id: str,
    context: ToolExecutionContext | None,
    *,
    scope: AgentTreeScope,
) -> bool:
    control = AgentControlPlane(context, runtime=runtime)
    if not control.has_actor_identity:
        return False
    return control.resolve_target(subagent_id, scope=scope) is not None


def _authorized_target(
    runtime: Any,
    subagent_id: str,
    context: ToolExecutionContext | None,
    *,
    scope: AgentTreeScope,
) -> tuple[AgentControlPlane, AgentTarget | None]:
    control = AgentControlPlane(context, runtime=runtime)
    if not control.has_actor_identity:
        return control, None
    return control, control.resolve_target(subagent_id, scope=scope)


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

        runtime = require_runtime_from_context(context)
        binding = canonical_agent_operation(self.name)
        control, target = _authorized_target(
            runtime,
            subagent_id,
            context,
            scope=binding.scope or "children",
        )
        if not control.can_use_operation(binding.operation):
            return ToolResult(
                content="Current agent execution profile does not allow interruption.",
                is_error=True,
                status="forbidden",
                result_kind="subagent",
            )
        if target is None:
            return ToolResult(content="Subagent is not owned by the current task.", is_error=True, status="forbidden", result_kind="subagent")
        outcome = control.interrupt(target)
        status = outcome.interrupt_status
        emit_event = context.emit_event if context else None

        if status == "cancelled":
            if emit_event is not None:
                await emit_event(
                    "subagent.progress",
                    AgentEvent.subagent_progress(
                        subagent_id=outcome.target.subagent_id,
                        detail="cancelling",
                        activity_kind="lifecycle",
                        activity_summary="正在停止子任务",
                        user_visible=True,
                    ).data,
                )
            return ToolResult(
                content=f"Cancellation requested for background subagent {outcome.target.subagent_id}.",
                display_summary=f"Cancelling {outcome.target.subagent_id}",
                result_kind="subagent",
                status="cancelled",
            )

        if status == "done":
            return ToolResult(
                content=f"Background subagent {outcome.target.subagent_id} has already finished.",
                display_summary=f"{outcome.target.subagent_id} already finished",
                result_kind="subagent",
                status="completed",
            )

        record = runtime.get_subagent(outcome.target.subagent_id)
        if record is not None and record.status != "running":
            return ToolResult(
                content=f"Subagent {outcome.target.subagent_id} is already {record.status}.",
                display_summary=f"{outcome.target.subagent_id} {record.status}",
                result_kind="subagent",
                status=record.status,
            )

        return ToolResult(
            content=f"No running background subagent found for {outcome.target.subagent_id}.",
            is_error=True,
            status="not_found",
            display_summary=f"{outcome.target.subagent_id} not found",
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

    def is_read_only(self, args: dict[str, Any] | None = None) -> bool:
        return not bool((args or {}).get("consume", False))

    def get_side_effect_kind(self, args: dict[str, Any] | None = None) -> str:
        # consume=true releases retained session result state and must not be
        # parallelized, retried, or cached as an observational call.
        from backend.tools.base import TOOL_SIDE_EFFECT_EXTERNAL, TOOL_SIDE_EFFECT_NONE

        return TOOL_SIDE_EFFECT_NONE if self.is_read_only(args) else TOOL_SIDE_EFFECT_EXTERNAL

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
                runtime = require_runtime_from_context(context)
                binding = canonical_agent_operation(self.name)
                scope = binding.scope or "children"
                control = AgentControlPlane(context, runtime=runtime)
                if not control.can_use_operation(binding.operation):
                    return ToolResult(
                        content="Current agent execution profile does not allow task observation.",
                        is_error=True,
                        status="forbidden",
                        result_kind="subagent",
                    )
                unauthorized = [
                    subagent_id
                    for subagent_id in subagent_ids
                    if not _authorized_subagent(
                        runtime,
                        subagent_id,
                        context,
                        scope=scope,
                    )
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
                    await control.wait_for_any(
                        subagent_ids,
                        timeout_seconds=wait_seconds,
                    )
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
        runtime = require_runtime_from_context(context)
        binding = canonical_agent_operation(self.name)
        control, target = _authorized_target(
            runtime,
            subagent_id,
            context,
            scope=binding.scope or "children",
        )
        if not control.can_use_operation(binding.operation):
            return ToolResult(
                content="Current agent execution profile does not allow task observation.",
                is_error=True,
                status="forbidden",
                result_kind="subagent",
            )
        if target is None:
            return ToolResult(content="Subagent is not owned by the current task.", is_error=True, status="forbidden", result_kind="subagent")
        subagent_id = target.subagent_id
        try:
            wait_seconds = max(0.0, min(float(args.get("wait_seconds") or 0), 600.0))
        except (TypeError, ValueError):
            wait_seconds = 0.0
        if wait_seconds:
            await control.wait_for_one(
                subagent_id,
                timeout_seconds=wait_seconds,
            )
        snapshot = control.subagent_snapshot(
            subagent_id,
            include_result=include_result,
        )
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
                control.forget_subagent_result(subagent_id)
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
        runtime = require_runtime_from_context(context)
        control = AgentControlPlane(context, runtime=runtime)
        binding = canonical_agent_operation(self.name)
        if not control.can_use_operation(binding.operation):
            return ToolResult(
                content="Current agent execution profile does not allow task observation.",
                is_error=True,
                status="forbidden",
                result_kind="subagent",
            )
        include_completed = bool(args.get("include_completed", True))
        try:
            limit = max(1, min(int(args.get("limit") or 20), 100))
        except (TypeError, ValueError):
            limit = 20
        candidates = control.select_agents(
            scope=binding.scope or "children",
            include_completed=include_completed,
            background_only=True,
        )
        # ``limit`` is applied after sorting, so it cannot be pushed into
        # select_agents without truncating the wrong subagents.
        items = list(candidates)
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
