"""
Dynamic MCP tool registration.

This module exposes MCP server tools through the main ToolRegistry so the
agent can call them like built-in tools. Tool names follow:
    mcp__{server_name}__{tool_name}
"""

from __future__ import annotations

import json
import logging
from collections import OrderedDict
from typing import Any

from backend.artifact.store import ArtifactStore
from backend.mcp.client import MCPCallResult, MCPClient, MCPToolDef
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema
from backend.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

# Store large MCP outputs as artifacts instead of sending the full text inline.
ARTIFACT_THRESHOLD = 2000


def _is_non_critical_mcp_timeout(server_name: str, tool_name: str, text: str) -> bool:
    if server_name not in {"memory-rag", "memory_rag"}:
        return False
    if tool_name not in {"remember", "recall"}:
        return False
    return "timed out" in text.lower() or "timeout" in text.lower() or "超时" in text


class MCPToolProxy(BaseTool):
    """Adapter that exposes an MCP tool through the local tool registry.

    Stores a *manager reference* instead of a direct client pointer so that
    reconnection (which creates a new MCPClient instance) is transparently
    picked up on the next ``execute()`` call.
    """

    # Class-level LRU cache for read-only tool results (shared across all instances).
    _result_cache: OrderedDict = OrderedDict()
    _RESULT_CACHE_MAX = 128

    def __init__(
        self,
        server_name: str,
        tool_def: MCPToolDef,
        client_or_manager: MCPClient | Any,
        artifact_store: ArtifactStore | None = None,
        manager: Any | None = None,
    ) -> None:
        self._server_name = server_name
        self._tool_def = tool_def
        self._artifact_store = artifact_store

        # Prefer the manager reference for dynamic client lookup (reconnection-safe).
        # Fall back to a direct client pointer for backward compatibility.
        self._manager = manager
        self._static_client: MCPClient | None = client_or_manager if manager is None else None

        self.name = f"mcp__{server_name}__{tool_def.name}"
        self.description = tool_def.description

        # Map MCP annotations/_meta to local capability hints (Phase 3.2).
        ann = getattr(tool_def, "annotations", {}) or {}
        meta = getattr(tool_def, "meta", {}) or {}
        self.read_only = bool(ann.get("readOnlyHint", False))
        self.destructive = bool(ann.get("destructiveHint", False))
        self.open_world = bool(ann.get("openWorldHint", False))
        self.always_load = bool(
            meta.get("anthropic/alwaysLoad") or meta.get("alwaysLoad") or False
        )

        # Permission: read-only → AUTO; destructive/open-world → CONFIRM.
        # memory-rag stays AUTO (local, low-risk). Unknown tools default CONFIRM.
        if self.read_only and not (self.destructive or self.open_world):
            self.permission = PermissionLevel.AUTO
        elif server_name in {"memory-rag", "memory_rag"}:
            self.permission = PermissionLevel.AUTO
        else:
            self.permission = PermissionLevel.CONFIRM

    @property
    def _client(self) -> MCPClient | None:
        """Dynamic client lookup — survives MCP server reconnections."""
        if self._manager is not None:
            return self._manager.get_client(self._server_name)
        return self._static_client

    def is_read_only(self, args: dict[str, Any] | None = None) -> bool:
        return self.read_only and not (self.destructive or self.open_world)

    def get_schema(self) -> ToolSchema:
        """Build a ToolSchema from the MCP tool definition."""
        params = self._tool_def.input_schema or {"type": "object", "properties": {}}
        description = (
            f"[MCP:{self._server_name}] {self._tool_def.description}\n"
            f"Original tool: {self._tool_def.name}"
        )
        return ToolSchema(
            name=self.name,
            description=description,
            parameters=params,
        )

    def get_spec(self):
        from backend.agent.harness.mcp_adapter import MCPToolSpecAdapter

        return MCPToolSpecAdapter.from_tool_def(self._server_name, self._tool_def).build_spec(self.name)

    async def execute(self, args: dict[str, Any]) -> ToolResult:
        """Execute the MCP tool and normalize the result into ToolResult.

        Read-only, non-destructive, closed-world tools benefit from an LRU
        result cache keyed on (server, tool, args) to avoid redundant MCP calls.
        """
        client = self._client
        if client is None or not client.connected:
            return self._error_result(
                f"MCP server '{self._server_name}' is not connected. "
                "Check .mcp.json or restart the server."
            )

        # --- result cache lookup (read-only tools only) ---
        cache_key: str | None = None
        if self.read_only and not (self.destructive or self.open_world):
            cache_key = (
                f"{self._server_name}::{self._tool_def.name}::"
                f"{json.dumps(args, sort_keys=True, ensure_ascii=False)}"
            )
            if cache_key in MCPToolProxy._result_cache:
                MCPToolProxy._result_cache.move_to_end(cache_key)
                return MCPToolProxy._result_cache[cache_key]

        try:
            result: MCPCallResult = await client.call_tool(self._tool_def.name, args)
        except Exception as exc:
            return self._error_result(f"MCP tool '{self._tool_def.name}' failed: {exc}")

        if result.is_error and _is_non_critical_mcp_timeout(self._server_name, self._tool_def.name, result.text or ""):
            return ToolResult(
                content=(
                    f"Optional MCP tool {self._server_name}/{self._tool_def.name} timed out. "
                    "Do not retry it in this turn; continue with the user-facing answer."
                ),
                is_error=False,
                status="timeout",
                limitation="non-critical timeout",
                display_summary=f"Optional MCP timed out: {self._server_name}/{self._tool_def.name}",
                result_kind="mcp",
            )

        if result.is_error:
            return self._error_result(result.text or "MCP tool execution failed")

        full_text = result.text
        if len(full_text) > ARTIFACT_THRESHOLD and self._artifact_store:
            artifact_id = self._artifact_store.save(
                content=full_text,
                source=self.name,
                type="mcp_result",
            )
            lines = full_text.split("\n")
            preview = "\n".join(lines[:5])
            summary = (
                f"MCP {self._server_name}.{self._tool_def.name} completed successfully\n"
                f"Returned {len(full_text)} chars across {len(lines)} lines"
            )
            result_obj = self._success_result(
                content=summary,
                artifact_id=artifact_id,
                artifact_preview=preview,
            )
        else:
            result_obj = self._success_result(content=full_text)

        # --- store in result cache (read-only tools only) ---
        if cache_key is not None:
            MCPToolProxy._result_cache[cache_key] = result_obj
            if len(MCPToolProxy._result_cache) > MCPToolProxy._RESULT_CACHE_MAX:
                MCPToolProxy._result_cache.popitem(last=False)

        return result_obj


class MCPToolRegistry:
    """Manage MCP tool proxy registration and cleanup."""

    def __init__(
        self,
        tool_registry: ToolRegistry,
        artifact_store: ArtifactStore | None = None,
        mcp_manager: Any | None = None,
    ) -> None:
        self._tool_registry = tool_registry
        self._artifact_store = artifact_store
        self._mcp_manager = mcp_manager
        self._server_tools: dict[str, list[str]] = {}
        # Cache for tool lists: server_name -> (version, tools_list)
        self._tool_list_cache: dict[str, tuple[int, list]] = {}

    def register_server_tools(
        self,
        server_name: str,
        tools: list[MCPToolDef],
        client: MCPClient,
    ) -> int:
        """Register all tools exposed by one MCP server."""
        self.unregister_server_tools(server_name)
        registered_names: list[str] = []

        for tool_def in tools:
            proxy = MCPToolProxy(
                server_name=server_name,
                tool_def=tool_def,
                client_or_manager=client,
                artifact_store=self._artifact_store,
                manager=self._mcp_manager,
            )
            self._tool_registry.register(proxy)
            registered_names.append(proxy.name)
            logger.info("[MCPRegistry] Registered tool: %s (from %s)", proxy.name, server_name)

        self._server_tools[server_name] = registered_names
        return len(registered_names)

    def unregister_server_tools(self, server_name: str) -> None:
        """Unregister all tools that came from one MCP server."""
        tool_names = self._server_tools.pop(server_name, [])
        for name in tool_names:
            self._tool_registry.unregister(name)
            logger.info("[MCPRegistry] Unregistered tool: %s", name)

    def get_server_tool_count(self, server_name: str) -> int:
        """Return the number of tools registered for one server."""
        return len(self._server_tools.get(server_name, []))

    def get_all_mcp_tools(self) -> dict[str, list[str]]:
        """Return the current server-to-tool mapping."""
        return dict(self._server_tools)
