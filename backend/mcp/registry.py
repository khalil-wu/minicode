"""
Dynamic MCP tool registration.

This module exposes MCP server tools through the main ToolRegistry so the
agent can call them like built-in tools. Tool names follow:
    mcp__{server_name}__{tool_name}
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from backend.artifact.store import ArtifactStore
from backend.mcp.client import MCPCallResult, MCPClient, MCPToolDef
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema
from backend.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

_BUNDLED_OPEN_WORLD_READERS = {
    ("websearch", "search"): ("-m", "backend.mcp.servers.websearch"),
    ("websearch", "fetch_page"): ("-m", "backend.mcp.servers.websearch"),
}

_BUNDLED_NATIVE_EQUIVALENTS = {
    ("websearch", "search"): "web_search",
    ("websearch", "fetch_page"): "web_fetch",
}

_BUNDLED_OWNER_SCOPED_TOOLS = {
    ("docparse", "parse"): ("-m", "backend.mcp.servers.docparse"),
    ("docparse", "get_section"): ("-m", "backend.mcp.servers.docparse"),
    ("docparse", "get_full_text"): ("-m", "backend.mcp.servers.docparse"),
}


def _is_trusted_bundled_reader(
    server_name: str,
    tool_name: str,
    client: MCPClient | Any,
) -> bool:
    """Trust only MiniCode's own stdio module, never extension annotations."""
    expected_args = _BUNDLED_OPEN_WORLD_READERS.get((server_name, tool_name))
    if expected_args is None:
        return False
    command = str(getattr(client, "_command", "") or "").strip()
    executable = Path(command).name.lower()
    args = tuple(str(arg) for arg in (getattr(client, "_args", None) or ()))
    return executable in {"python", "python.exe", "python3", "python3.exe"} and args == expected_args


def _is_trusted_owner_scoped_tool(
    server_name: str,
    tool_name: str,
    client: MCPClient | Any,
) -> bool:
    expected_args = _BUNDLED_OWNER_SCOPED_TOOLS.get((server_name, tool_name))
    if expected_args is None:
        return False
    command = str(getattr(client, "_command", "") or "").strip()
    executable = Path(command).name.lower()
    args = tuple(str(arg) for arg in (getattr(client, "_args", None) or ()))
    return executable in {"python", "python.exe", "python3", "python3.exe"} and args == expected_args


class MCPToolProxy(BaseTool):
    """Adapter that exposes an MCP tool through the local tool registry.

    Stores a *manager reference* instead of a direct client pointer so that
    reconnection (which creates a new MCPClient instance) is transparently
    picked up on the next ``execute()`` call.
    """

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
        self._trusted_bundled_reader = _is_trusted_bundled_reader(
            server_name,
            tool_def.name,
            client_or_manager,
        )
        self.always_load = bool(
            meta.get("anthropic/alwaysLoad") or meta.get("alwaysLoad") or False
        )

        # Permission: read-only → AUTO; destructive/open-world → CONFIRM.
        # Unknown tools default CONFIRM.
        if self.read_only and not (self.destructive or self.open_world):
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
        # Extension metadata is untrusted. The bundled web reader is the only
        # open-world exception because its implementation is owned by MiniCode;
        # approval floors remain independent in PermissionChecker.
        return self.read_only and not self.destructive and (
            not self.open_world or self._trusted_bundled_reader
        )

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
        from backend.mcp.tool_spec_adapter import MCPToolSpecAdapter

        return MCPToolSpecAdapter.from_tool_def(self._server_name, self._tool_def).build_spec(self.name)

    async def execute(self, args: dict[str, Any], *, context: Any | None = None) -> ToolResult:
        """Execute the MCP tool and normalize the result into ToolResult.

        Tool annotations describe permission risk, not result immutability.
        Every invocation reaches the server so mutable remote state stays fresh.
        """
        client = self._client
        if client is None or not client.connected:
            return self._error_result(
                f"MCP server '{self._server_name}' is not connected. "
                "Check .mcp.json or restart the server."
            )

        try:
            request_owner = None
            if context is not None:
                metadata = getattr(context, "metadata", {}) or {}
                request_owner = {
                    "session_id": str(getattr(context, "session_id", "") or ""),
                    "conversation_id": str(getattr(context, "conversation_id", "") or ""),
                    "task_id": str(getattr(context, "task_id", "") or ""),
                    "run_id": str(metadata.get("run_id") or ""),
                    "rollout_budget": metadata.get("_rollout_budget"),
                    "deadline_monotonic": getattr(context, "deadline_monotonic", None),
                    # Server-initiated MCP requests inherit the exact tool-call
                    # owner.  Keeping the cancellation object here lets sampling
                    # and elicitation stop with the originating turn instead of
                    # outliving a cancelled tool call in the shared MCP client.
                    "cancel_event": getattr(context, "cancel_event", None),
                }
            request_meta = None
            if context is not None and _is_trusted_owner_scoped_tool(
                self._server_name,
                self._tool_def.name,
                client,
            ):
                request_meta = {
                    "minicode.dev/owner": {
                        "conversation_id": str(getattr(context, "conversation_id", "") or ""),
                        "workspace_root": str(getattr(context, "workspace_root", "") or ""),
                    },
                }
            if request_meta:
                result: MCPCallResult = await client.call_tool(
                    self._tool_def.name,
                    args,
                    request_owner=request_owner,
                    request_meta=request_meta,
                )
            else:
                result = await client.call_tool(
                    self._tool_def.name,
                    args,
                    request_owner=request_owner,
                )
        except Exception as exc:
            return self._error_result(f"MCP tool '{self._tool_def.name}' failed: {exc}")

        result_text = result.summary_text
        if result.is_error:
            return self._error_result(result_text or "MCP tool execution failed")

        # Result sizing and artifact promotion belong to the shared tool
        # execution path (Pi's 2,000-line/50-KiB contract). Keeping an MCP-only
        # character threshold here caused a second, undocumented truncation
        # policy and made the same tool behave differently through MCP.
        return self._success_result(content=result_text)


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
            native_equivalent = _BUNDLED_NATIVE_EQUIVALENTS.get(
                (server_name, tool_def.name)
            )
            if (
                native_equivalent
                and self._tool_registry.has_tool(native_equivalent)
                and _is_trusted_bundled_reader(server_name, tool_def.name, client)
            ):
                logger.info(
                    "[MCPRegistry] Skipped legacy bundled tool %s/%s; native %s is registered",
                    server_name,
                    tool_def.name,
                    native_equivalent,
                )
                continue
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
