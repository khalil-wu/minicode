"""Provider-stream diagnostics for settled tool calls."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from backend.llm.base import ToolCallEvent


class StreamingToolStatus(str, Enum):
    QUEUED = "queued"
    YIELDED = "yielded"
    CANCELLED = "cancelled"


@dataclass(slots=True)
class StreamingToolRecord:
    tool_call: ToolCallEvent
    status: StreamingToolStatus = StreamingToolStatus.QUEUED


class StreamingToolTracker:
    """Track provider tool-call order without starting speculative execution."""

    def __init__(self) -> None:
        self.tracked_tools: dict[str, StreamingToolRecord] = {}

    def add_tool(self, tool_call: ToolCallEvent) -> None:
        tool_call_id = str(tool_call.id or "")
        if tool_call_id and tool_call_id not in self.tracked_tools:
            self.tracked_tools[tool_call_id] = StreamingToolRecord(tool_call=tool_call)

    def add_tools(self, tool_calls: list[ToolCallEvent]) -> None:
        for tool_call in tool_calls:
            self.add_tool(tool_call)

    def status_snapshot(self) -> tuple[tuple[str, str], ...]:
        return tuple(
            (tool_id, record.status.value)
            for tool_id, record in self.tracked_tools.items()
        )

    def mark_yielded(self, tool_call_id: str) -> None:
        record = self.tracked_tools.get(str(tool_call_id or ""))
        if record is not None:
            record.status = StreamingToolStatus.YIELDED

    def cancel_remaining(self) -> None:
        for record in self.tracked_tools.values():
            if record.status is not StreamingToolStatus.YIELDED:
                record.status = StreamingToolStatus.CANCELLED
        self.tracked_tools.clear()


__all__ = [
    "StreamingToolRecord",
    "StreamingToolStatus",
    "StreamingToolTracker",
]
