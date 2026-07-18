from __future__ import annotations

from typing import Any

from backend.agent.message import AgentEvent
from backend.agent.state import AgentState
from backend.agent.tool_projection import DEFAULT_PROJECTION_REGISTRY
from backend.agent.tool_runtime import tool_is_idempotent, tool_side_effect_kind
from backend.llm.base import ToolCallEvent
from backend.tools.base import ToolResult
from backend.tools.registry import ToolRegistry


def describe_tool_call(tc: ToolCallEvent) -> str:
    args = tc.arguments or {}
    target = (
        str(args.get("file_path") or args.get("path") or args.get("target") or "").strip()
        or str(args.get("directory") or args.get("cwd") or "").strip()
        or str(args.get("pattern") or args.get("query") or "").strip()
        or str(args.get("command") or "").strip()
    )
    return f"{tc.name} {target}".strip()


def status_for_result(result: ToolResult, requested_status: str | None = None) -> str:
    if requested_status in {"success", "failed", "blocked", "partial", "timeout"}:
        return requested_status
    if result.status == "timeout" and result.limitation == "non-critical timeout":
        return "success"
    if result.status in {"success", "failed", "blocked", "partial", "timeout"}:
        return str(result.status)
    return "failed" if result.is_error else "success"


def tool_start_times(state: AgentState) -> dict[str, float]:
    existing = getattr(state, "_ui_tool_started_at", None)
    if isinstance(existing, dict):
        return existing
    created: dict[str, float] = {}
    setattr(state, "_ui_tool_started_at", created)
    return created


def tool_call_start_event(
    tc: ToolCallEvent,
    *,
    started_epoch: float,
    iteration_id: str,
    tool_registry: ToolRegistry | None = None,
    turn_id: str = "",
) -> AgentEvent:
    projection = DEFAULT_PROJECTION_REGISTRY.project_tool_call(tc.name, tc.arguments)
    side_effect_kind = tool_side_effect_kind(tc.name, tool_registry, tc.arguments) if tool_registry is not None else ""
    idempotent = tool_is_idempotent(tc.name, tool_registry, tc.arguments) if tool_registry is not None else None
    idempotency_key = ""
    if tool_registry is not None:
        tool = tool_registry.get_tool(tc.name)
        get_key = getattr(tool, "idempotency_key", None)
        if callable(get_key):
            try:
                idempotency_key = str(get_key(tc.arguments) or "")
            except Exception:
                idempotency_key = ""
    return AgentEvent.tool_call(
        id=tc.id,
        name=tc.name,
        args=tc.arguments,
        started_at=int(started_epoch * 1000),
        display_hint=projection.display_hint,
        input_summary=projection.input_summary,
        result_kind=projection.result_kind,
        activity_kind=projection.activity_kind,
        group_id=iteration_id,
        step_id=tc.id,
        turn_id=turn_id,
        iteration_id=iteration_id,
        phase="tool",
        display_scope=projection.display_scope,
        panel_hint=projection.panel_hint,
        requires_attention=projection.requires_attention,
        side_effect_kind=side_effect_kind,
        idempotent=idempotent,
        idempotency_key=idempotency_key,
    )


def panel_hint_for_tool_result(tool_name: str, result_kind: str, diff: dict[str, Any] | None) -> str:
    if result_kind == "edit" or diff is not None:
        return "diff"
    if result_kind == "subagent" or tool_name == "task":
        return "subagents"
    return "inspector"


def requires_attention_for_tool_result(
    *,
    final_status: str,
    projection: str,
    result_kind: str,
    diff: dict[str, Any] | None,
) -> bool:
    if projection in {"approval", "error"}:
        return True
    if final_status in {"failed", "blocked", "timeout"} and projection not in {"silent", "status", "warning"}:
        return True
    if diff is not None and result_kind == "edit":
        return False
    return False
