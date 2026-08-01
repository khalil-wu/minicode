from __future__ import annotations

from backend.agent.message import AgentEvent
from backend.agent.state import AgentState
from backend.agent.tool_projection import projection_for_tool
from backend.agent.tool_runtime import tool_is_idempotent, tool_side_effect_kind
from backend.llm.base import ToolCallEvent, ToolCallStartEvent
from backend.tools.base import ToolResult
from backend.tools.registry import ToolRegistry


def status_for_result(result: ToolResult, requested_status: str | None = None) -> str:
    if requested_status in {"success", "failed", "blocked", "partial", "timeout", "cancelled"}:
        return requested_status
    if result.status in {"success", "failed", "blocked", "partial", "timeout", "cancelled"}:
        return str(result.status)
    return "failed" if result.is_error else "success"


def tool_start_times(state: AgentState) -> dict[str, float]:
    return state.ui_tool_started_at


def tool_call_pending_event(
    start: ToolCallStartEvent,
    *,
    started_at: int,
    iteration_id: str,
    tool_registry: ToolRegistry | None = None,
) -> AgentEvent:
    """Project a provider tool block before its streamed arguments are complete."""

    projection = projection_for_tool(start.name, tool_registry)
    return AgentEvent.tool_call(
        id=start.id,
        name=start.name,
        args={},
        status="pending",
        started_at=started_at,
        display_hint=projection.display_hint,
        result_kind=projection.result_kind,
        activity_kind=projection.activity_kind,
        group_id=iteration_id,
        step_id=start.id,
        iteration_id=iteration_id,
        phase="tool",
    )


def tool_call_start_event(
    tc: ToolCallEvent,
    *,
    started_epoch: float,
    iteration_id: str,
    tool_registry: ToolRegistry | None = None,
    turn_id: str = "",
) -> AgentEvent:
    projection = projection_for_tool(tc.name, tool_registry)
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
        result_kind=projection.result_kind,
        activity_kind=projection.activity_kind,
        group_id=iteration_id,
        step_id=tc.id,
        turn_id=turn_id,
        iteration_id=iteration_id,
        phase="tool",
        side_effect_kind=side_effect_kind,
        idempotent=idempotent,
        idempotency_key=idempotency_key,
    )
