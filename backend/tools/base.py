"""Base abstractions for tools exposed to the agent."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from backend.permissions.context import ToolExecutionContext


class PermissionLevel(Enum):
    """Permission level required before a tool can run."""

    AUTO = "auto"
    CONFIRM = "confirm"
    DIFF_REVIEW = "diff"
    ALWAYS_DENY = "deny"


@dataclass
class ToolResult:
    """Normalized tool execution result.

    content is the compact text injected into agent context. Oversized outputs
    should be stored as artifacts and represented here by artifact_id plus a
    short preview.
    """

    content: str
    artifact_id: str | None = None
    artifact_preview: str | None = None
    is_error: bool = False
    source_url: str | None = None
    extraction_status: str | None = None
    content_preview: str | None = None
    evidence_type: str | None = None
    display_summary: str | None = None
    result_kind: str | None = None
    limitation: str | None = None
    duration_ms: int | None = None
    status: str | None = None

    def to_context_string(self) -> str:
        """Return the compact representation injected into model context."""
        parts: list[str] = []
        if self.evidence_type:
            parts.append(f"[evidence: {self.evidence_type}]")
        if self.source_url:
            parts.append(f"[source: {self.source_url}]")
        if self.extraction_status:
            parts.append(f"[extraction: {self.extraction_status}]")
        if self.limitation:
            parts.append(f"[limitation: {self.limitation}]")
        parts.append(self.content)
        if self.content_preview:
            parts.append(f"--- content preview ---\n{self.content_preview}")
        elif self.artifact_preview:
            parts.append(f"--- artifact preview ---\n{self.artifact_preview}")
        if self.artifact_id:
            parts.append(
                f"Full result saved as artifact. Use read_artifact('{self.artifact_id}') if more detail is needed."
            )
        return "\n".join(parts)


MAX_TOOL_RESULT_CHARS = 12_000


def truncate_tool_result(content: str, max_chars: int = MAX_TOOL_RESULT_CHARS) -> str:
    """Truncate long tool output while keeping head and tail context."""
    if len(content) <= max_chars:
        return content
    head_len = int(max_chars * 0.8)
    tail_len = int(max_chars * 0.1)
    head = content[:head_len]
    tail = content[-tail_len:]
    omitted = len(content) - head_len - tail_len
    return f"{head}\n\n... [{omitted} chars truncated] ...\n\n{tail}"


@dataclass
class ToolSchema:
    """JSON schema definition for an agent tool."""

    name: str
    description: str
    parameters: dict[str, Any]
    strict: bool = False

    def to_openai_tool(self) -> dict[str, Any]:
        """Convert to OpenAI-compatible function-calling format."""
        tool: dict[str, Any] = {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }
        if self.strict:
            tool["function"]["strict"] = True
        return tool

    def to_summary(self) -> str:
        """Return a compact one-line summary for constrained context budgets."""
        return f"- {self.name}: {self.description.split('.')[0]}"


class BaseTool(ABC):
    """Base class for all tools."""

    name: str
    description: str
    permission: PermissionLevel = PermissionLevel.AUTO
    read_only: bool = False

    def is_concurrency_safe(self, args: dict[str, Any] | None = None) -> bool:
        """Return whether this tool can safely run alongside other read-only work."""
        if self.read_only:
            return True
        return self.name in {
            "read_file",
            "list_files",
            "grep_files",
            "glob_files",
            "fuzzy_search",
            "git_status",
            "git_diff",
            "git_log",
            "go_to_definition",
            "find_references",
            "read_artifact",
            "tool_search",
        }

    @abstractmethod
    def get_schema(self) -> ToolSchema:
        """Return the JSON schema for this tool."""

    @abstractmethod
    async def execute(
        self,
        args: dict[str, Any],
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        """Execute the tool and return a compact, human-readable result."""

    def _error_result(self, message: str) -> ToolResult:
        """Return a standardized error result."""
        return ToolResult(content=f"Error: {message}", is_error=True)

    def _success_result(
        self,
        content: str,
        artifact_id: str | None = None,
        artifact_preview: str | None = None,
    ) -> ToolResult:
        """Return a standardized success result."""
        return ToolResult(
            content=content,
            artifact_id=artifact_id,
            artifact_preview=artifact_preview,
        )
