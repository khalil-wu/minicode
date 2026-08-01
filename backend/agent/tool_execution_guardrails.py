"""Rejection results for tool calls that never reach execution.

Every pre-execution guard in ``tool_execution`` funnels its refusal through
these two builders so a blocked call reaches the model and the UI in the same
shape as a real result: a ``ToolResult`` carrying a model-facing observation,
a user-facing summary, and a projection hint.

The guards themselves (scope, disabled, validation, permission) live in
``tool_execution`` next to the batch executor that owns their ordering.
"""

from __future__ import annotations

from backend.llm.base import ToolCallEvent
from backend.tools.base import ToolResult


def rejection_result(
    tc: ToolCallEvent,
    message: str,
    *,
    is_error: bool = True,
    display_summary: str = "Tool call rejected",
    result_kind: str = "generic",
    error_kind: str = "execution_error",
    user_summary: str = "工具调用未完成。",
    developer_detail: str = "",
    recoverable: bool = True,
    projection: str = "error",
    model_observation: str = "",
) -> ToolResult:
    """Create a ToolResult for a tool call rejected before execution."""
    return ToolResult(
        content=str(message or "Tool call rejected"),
        is_error=is_error,
        display_summary=display_summary,
        result_kind=result_kind,
        status="blocked" if is_error else "completed",
        error_kind=error_kind,
        user_summary=user_summary,
        developer_detail=developer_detail or str(message or ""),
        recoverable=recoverable,
        projection=projection,
        model_observation=model_observation,
    )


def invalid_call_result(
    tc: ToolCallEvent,
    reason: str,
) -> ToolResult:
    """Create a consistent ToolResult for malformed/invalid model tool calls."""
    return rejection_result(
        tc,
        reason,
        is_error=True,
        display_summary="Invalid tool call",
        result_kind="generic",
        error_kind="validation_error",
        user_summary="工具调用缺少必要参数或格式无效。",
        projection="error",
        model_observation=f"The {tc.name} call has invalid or incomplete arguments.",
    )
