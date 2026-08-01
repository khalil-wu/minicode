from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from backend.llm.base import ToolCallEvent
from backend.tools.base import ToolResult

if TYPE_CHECKING:
    from backend.tools.registry import ToolRegistry


@dataclass(frozen=True)
class ToolProjection:
    """Tool-owned metadata used by the UI protocol."""

    result_kind: str
    display_hint: str
    activity_kind: str = ""


def projection_for_tool(
    tool_name: str,
    tool_registry: "ToolRegistry | None" = None,
) -> ToolProjection:
    metadata: dict[str, Any] = {}
    if tool_registry is not None:
        tool = tool_registry.get_tool(tool_name)
        if tool is not None:
            metadata = tool.to_projection_metadata()
    return ToolProjection(
        result_kind=str(metadata.get("result_kind") or "generic"),
        display_hint=str(metadata.get("display_label") or tool_name),
        activity_kind=str(metadata.get("activity_kind") or "genericTool"),
    )


def result_kind_for_tool(
    tool_name: str,
    tool_registry: "ToolRegistry | None" = None,
) -> str:
    return projection_for_tool(tool_name, tool_registry=tool_registry).result_kind


def display_summary_for_result(
    tc: ToolCallEvent,
    result: ToolResult,
    *,
    status: str,
    diff: dict[str, Any] | None = None,
    tool_registry: "ToolRegistry | None" = None,
) -> str:
    del diff
    if result.display_summary:
        return result.display_summary
    label = projection_for_tool(tc.name, tool_registry).display_hint
    if status == "blocked":
        return f"Blocked: {label}"
    if status in {"failed", "timeout", "cancelled"}:
        return f"Failed: {label}"
    return f"Completed: {label}"
