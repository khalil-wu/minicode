from __future__ import annotations

from typing import Any

from backend.agent.message import AgentEvent
from backend.agent.state import AgentState
from backend.agent.tool_projection import projection_for_tool
from backend.agent.tool_runtime import tool_is_idempotent, tool_side_effect_kind
from backend.agent.tool_stream_tracker import StreamingToolStatus, StreamingToolTracker
from backend.agent.final_tool_request import canonical_tool_request_digest
from backend.llm.base import ToolCallEvent, ToolCallStartEvent
from backend.tools.base import ToolResult
from backend.tools.registry import ToolRegistry


def status_for_result(result: ToolResult, requested_status: str | None = None) -> str:
    if requested_status in {
        "success",
        "failed",
        "blocked",
        "partial",
        "timeout",
        "cancelled",
    }:
        return requested_status
    if result.status in {
        "success",
        "failed",
        "blocked",
        "partial",
        "timeout",
        "cancelled",
    }:
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
        visibility=projection.visibility,
        group_id=iteration_id,
        step_id=start.id,
        iteration_id=iteration_id,
        phase="tool",
    )


def abandoned_tool_announcement_events(
    stream_state: Any,
    *,
    iteration_id: str = "",
) -> list[AgentEvent]:
    """Close every tool block the provider announced but never completed.

    Without this the truncated call stays pending for the rest of the run and
    the turn projection settles it as a bare failure: no arguments, no summary,
    no reason. Say what happened instead - the call never ran, and a
    continuation attempt may re-issue it with complete arguments.
    """

    events: list[AgentEvent] = []
    for tool_id, name in stream_state.take_unsettled_tool_announcements():
        label = str(name or "").strip()
        events.append(
            AgentEvent.tool_result(
                id=tool_id,
                summary=(
                    f"Provider 在写完 {label or '该工具'} 的参数前中断了这次调用，"
                    "工具没有执行。"
                ),
                is_error=True,
                status="cancelled",
                tool_name=label,
                display_summary="未执行：调用被截断",
                error_kind="incomplete_tool_stream",
                user_summary="该工具调用未执行（参数被截断）。",
                developer_detail=(
                    "The provider announced this tool block and stopped before the "
                    "arguments were complete; it was never executed."
                ),
                recoverable=True,
                group_id=iteration_id,
                step_id=tool_id,
                iteration_id=iteration_id,
                phase="tool",
            )
        )
    return events


def cancelled_pending_tool_events(
    stream_state: Any,
    tool_tracker: StreamingToolTracker,
    *,
    iteration_id: str = "",
) -> list[AgentEvent]:
    """Close complete calls that were collected but never entered execution.

    A provider can deliver a complete ``TOOL_CALL`` and then be cancelled in
    the hand-off window before ``execute_tool_turn`` owns the batch. The
    partial-announcement helper cannot see those calls because their provider
    arguments are complete, so settle the executor's queued records here.
    """

    tracked_tools = tool_tracker.tracked_tools
    events: list[AgentEvent] = []
    settled_ids: set[str] = set()
    for tool_call in list(getattr(stream_state, "tool_calls", ())):
        call_id = str(getattr(tool_call, "id", "") or "").strip()
        if not call_id or call_id in settled_ids:
            continue
        record = tracked_tools.get(call_id)
        if record is not None and record.status is not StreamingToolStatus.QUEUED:
            continue
        settled_ids.add(call_id)
        tool_name = str(getattr(tool_call, "name", "") or "").strip()
        events.append(
            AgentEvent.tool_result(
                id=call_id,
                summary=(
                    f"The provider call for {tool_name or 'the tool'} was cancelled "
                    "before tool execution began."
                ),
                is_error=True,
                status="cancelled",
                tool_name=tool_name,
                display_summary="Not executed: turn cancelled",
                error_kind="turn_cancelled_before_tool_execution",
                user_summary="工具调用尚未执行，本轮已取消。",
                developer_detail=(
                    "The provider emitted a complete tool call, but turn cancellation "
                    "arrived before the ordered tool transition started."
                ),
                recoverable=True,
                group_id=iteration_id,
                step_id=call_id,
                iteration_id=iteration_id,
                phase="tool",
            )
        )
    return events


def tool_call_start_event(
    tc: ToolCallEvent,
    *,
    started_epoch: float,
    iteration_id: str,
    tool_registry: ToolRegistry | None = None,
    turn_id: str = "",
) -> AgentEvent:
    projection = projection_for_tool(tc.name, tool_registry)
    side_effect_kind = (
        tool_side_effect_kind(tc.name, tool_registry, tc.arguments)
        if tool_registry is not None
        else ""
    )
    idempotent = (
        tool_is_idempotent(tc.name, tool_registry, tc.arguments)
        if tool_registry is not None
        else None
    )
    idempotency_key = ""
    if tool_registry is not None:
        tool = tool_registry.get_tool(tc.name)
        get_key = getattr(tool, "idempotency_key", None)
        if callable(get_key):
            try:
                idempotency_key = str(get_key(tc.arguments) or "")
            except Exception:
                idempotency_key = ""
    request_digest = str(
        getattr(tc, "_final_request_digest", "")
        or canonical_tool_request_digest(tc.name, tc.arguments or {})
    ).strip()
    return AgentEvent.tool_call(
        id=tc.id,
        name=tc.name,
        args=tc.arguments,
        started_at=int(started_epoch * 1000),
        display_hint=projection.display_hint,
        result_kind=projection.result_kind,
        activity_kind=projection.activity_kind,
        visibility=projection.visibility,
        group_id=iteration_id,
        step_id=tc.id,
        turn_id=turn_id,
        iteration_id=iteration_id,
        phase="tool",
        side_effect_kind=side_effect_kind,
        idempotent=idempotent,
        idempotency_key=idempotency_key,
        request_digest=request_digest,
    )
