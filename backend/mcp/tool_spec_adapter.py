from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from backend.tools.contracts import ToolSpec


@dataclass(frozen=True)
class MCPToolSpecAdapter:
    """Expose an MCP server's declared schema without semantic inference.

    MCP servers are dynamic and untrusted from the agent runtime point of view.
    Every installed tool remains discoverable through the deferred catalog;
    permission checks, approval policy, and the server schema decide whether a
    discovered call may execute.
    """

    server_name: str
    tool_name: str
    description: str = ""
    input_schema: dict[str, Any] | None = None
    annotations: dict[str, Any] | None = None

    @classmethod
    def from_tool_def(cls, server_name: str, tool_def: Any) -> "MCPToolSpecAdapter":
        return cls(
            server_name=server_name,
            tool_name=str(getattr(tool_def, "name", "") or ""),
            description=str(getattr(tool_def, "description", "") or ""),
            input_schema=getattr(tool_def, "input_schema", None) or {},
            annotations=getattr(tool_def, "annotations", None) or {},
        )

    def build_spec(self, runtime_name: str) -> ToolSpec:
        required = self._required_args()
        return ToolSpec(
            name=runtime_name,
            capability="mcp",
            toolset="mcp",
            exposure="deferred",
            required_args=required,
        )

    def _required_args(self) -> tuple[str, ...]:
        schema = self.input_schema or {}
        required = schema.get("required", [])
        return tuple(str(field) for field in required if isinstance(field, str))
