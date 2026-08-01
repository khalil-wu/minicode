from __future__ import annotations

from backend.tools.contracts import ToolIssue
from backend.llm.base import ToolCallEvent
from backend.tools.base import ToolResult


def classify_tool_issue(tc: ToolCallEvent, result: ToolResult, status: str) -> ToolIssue | None:
    detail = str(result.content or "").strip()
    tool_name = str(tc.name or "tool")

    if result.error_kind or result.user_summary or result.developer_detail or result.projection or result.model_observation:
        return ToolIssue(
            error_kind=str(result.error_kind or "execution_error"),
            user_summary=str(result.user_summary or "工具执行失败。"),
            developer_detail=str(result.developer_detail or detail),
            recoverable=bool(result.recoverable),
            projection=str(result.projection or "error"),
            model_observation=str(result.model_observation or ""),
        )

    if status not in {"error", "failed", "blocked"} and not result.is_error:
        return None
    return ToolIssue(
        error_kind="execution_error",
        user_summary="工具执行失败。",
        developer_detail=detail,
        projection="error",
        model_observation=f"The {tool_name} call failed. Try another approach if possible.",
    )
