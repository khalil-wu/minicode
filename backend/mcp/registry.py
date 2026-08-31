"""
Dynamic MCP tool registration.

This module exposes MCP server tools through the main ToolRegistry so the
agent can call them like built-in tools. Tool names follow:
    mcp__{server_name}__{tool_name}
"""

from __future__ import annotations

import logging
import re
from typing import Any

from backend.artifact.store import ArtifactStore
from backend.mcp.client import MCPCallResult, MCPClient, MCPToolDef
from backend.tools.base import (
    BaseTool,
    PermissionLevel,
    TOOL_SIDE_EFFECT_DESTRUCTIVE,
    TOOL_SIDE_EFFECT_EXTERNAL,
    TOOL_SIDE_EFFECT_NONE,
    ToolResult,
    ToolSchema,
)
from backend.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


def normalize_name_for_mcp(name: str) -> str:
    """Claude Code's wire-name normalization for MCP servers and tools."""
    return re.sub(r"[^a-zA-Z0-9_-]", "_", str(name or ""))


# Claude Code MAX_MCP_DESCRIPTION_LENGTH (client.ts:218): tool descriptions
# longer than this are truncated with a marker before reaching the model.
MAX_MCP_DESCRIPTION_LENGTH = 2048


def _truncate_mcp_description(description: str) -> str:
    desc = str(description or "")
    if len(desc) <= MAX_MCP_DESCRIPTION_LENGTH:
        return desc
    return desc[:MAX_MCP_DESCRIPTION_LENGTH] + "… [truncated]"

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
        self._catalog_revision = (
            int(getattr(manager, "registry_version", 0) or 0)
            if manager is not None
            else 0
        )

        self.name = (
            f"mcp__{normalize_name_for_mcp(server_name)}__"
            f"{normalize_name_for_mcp(tool_def.name)}"
        )
        self.description = _truncate_mcp_description(tool_def.description)
        # Keep the qualified protocol name for execution and policy matching,
        # while projecting MCP calls the same way Codex does in its activity UI.
        self.activity_kind = "mcpToolCall"
        self.display_label = f"{server_name}.{tool_def.name}"

        # Map MCP annotations/_meta to local capability hints (Phase 3.2).
        ann = getattr(tool_def, "annotations", {}) or {}
        meta = getattr(tool_def, "meta", {}) or {}
        self.read_only = bool(ann.get("readOnlyHint", False))
        self.destructive = bool(ann.get("destructiveHint", False))
        self.open_world = bool(ann.get("openWorldHint", False))
        # MCP _meta vendor extensions are namespaced; "anthropic/alwaysLoad" is
        # the only key with a cross-harness convention (cc Tool.ts).
        self.always_load = bool(meta.get("anthropic/alwaysLoad"))

        server_config = (
            manager.get_server_config(server_name)
            if manager is not None and hasattr(manager, "get_server_config")
            else None
        )
        self._supports_parallel_tool_calls = bool(
            getattr(server_config, "supports_parallel_tool_calls", False)
        )
        tool_modes = getattr(server_config, "tool_approval_modes", {}) or {}
        approval_mode = tool_modes.get(tool_def.name) or getattr(
            server_config,
            "default_tools_approval_mode",
            None,
        ) or "auto"
        self._approval_mode = approval_mode

        auto_requires_approval = not (
            self.read_only and not (self.destructive or self.open_world)
        )
        if approval_mode == "approve":
            requires_approval = False
        elif approval_mode == "prompt":
            requires_approval = True
        elif approval_mode == "writes":
            requires_approval = not self.read_only
        else:
            requires_approval = auto_requires_approval
        self.permission = (
            PermissionLevel.CONFIRM if requires_approval else PermissionLevel.AUTO
        )

    @property
    def _client(self) -> MCPClient | None:
        """Dynamic client lookup — survives MCP server reconnections."""
        if self._manager is not None:
            return self._manager.get_client(self._server_name)
        return self._static_client

    def is_read_only(self, args: dict[str, Any] | None = None) -> bool:
        # Open-world MCP calls remain externally observable even when a server
        # advertises readOnlyHint, so they are not safe read-only subagent work.
        return self.read_only and not self.destructive and not self.open_world

    def get_side_effect_kind(self, args: dict[str, Any] | None = None) -> str:
        # Remote tools that are not positively identified as reads are external
        # mutations. MCP annotations may advertise a read, but an absent hint
        # must never become side-effect-free through the BaseTool default.
        if self.destructive:
            return TOOL_SIDE_EFFECT_DESTRUCTIVE
        if self.is_read_only(args):
            return TOOL_SIDE_EFFECT_NONE
        return TOOL_SIDE_EFFECT_EXTERNAL

    def is_concurrency_safe(self, args: dict[str, Any] | None = None) -> bool:
        if self._supports_parallel_tool_calls:
            return True
        return super().is_concurrency_safe(args)

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

    def _catalog_is_current(self) -> bool:
        """Reject a call whose model-visible MCP schema crossed a catalog change.

        Codex binds prepared MCP calls to a catalog revision.  Keep the same
        fail-closed rule while allowing status-only reconnects when the tool
        definition is unchanged.
        """
        if self._manager is None:
            return True
        current_revision = int(getattr(self._manager, "registry_version", 0) or 0)
        if current_revision == self._catalog_revision:
            return True
        get_all_tools = getattr(self._manager, "get_all_tools", None)
        if not callable(get_all_tools):
            return False
        current_tools = (get_all_tools() or {}).get(self._server_name, ())
        current = next(
            (item for item in current_tools if str(getattr(item, "name", "")) == self._tool_def.name),
            None,
        )
        if current is None:
            return False
        current_server_config = (
            self._manager.get_server_config(self._server_name)
            if hasattr(self._manager, "get_server_config")
            else None
        )
        current_tool_modes = getattr(current_server_config, "tool_approval_modes", {}) or {}
        current_approval_mode = current_tool_modes.get(self._tool_def.name) or getattr(
            current_server_config,
            "default_tools_approval_mode",
            None,
        ) or "auto"
        same_contract = (
            dict(getattr(current, "input_schema", {}) or {})
            == dict(getattr(self._tool_def, "input_schema", {}) or {})
            and str(getattr(current, "description", "") or "")
            == str(getattr(self._tool_def, "description", "") or "")
            and dict(getattr(current, "annotations", {}) or {})
            == dict(getattr(self._tool_def, "annotations", {}) or {})
            and dict(getattr(current, "meta", {}) or {})
            == dict(getattr(self._tool_def, "meta", {}) or {})
            and bool(getattr(current_server_config, "supports_parallel_tool_calls", False))
            == self._supports_parallel_tool_calls
            and current_approval_mode == self._approval_mode
        )
        if same_contract:
            self._catalog_revision = current_revision
        return same_contract

    async def execute(self, args: dict[str, Any], *, context: Any | None = None) -> ToolResult:
        """Execute the MCP tool and normalize the result into ToolResult.

        Tool annotations describe permission risk, not result immutability.
        Every invocation reaches the server so mutable remote state stays fresh.
        """
        if not self._catalog_is_current():
            return self._error_result(
                f"MCP catalog changed for '{self._server_name}'. Refresh tools before calling '{self._tool_def.name}'."
            )
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
                run_context = getattr(context, "run_context", None)
                request_owner = {
                    "session_id": str(
                        getattr(run_context, "mcp_owner_session_id", "")
                        or getattr(context, "session_id", "")
                        or ""
                    ),
                    "conversation_id": str(getattr(context, "conversation_id", "") or ""),
                    "task_id": str(getattr(context, "task_id", "") or ""),
                    "run_id": str(metadata.get("run_id") or ""),
                    "conversation_run_generation": metadata.get(
                        "conversation_run_generation"
                    ),
                    "mcp_manager": (
                        getattr(run_context, "mcp_manager", None)
                        or self._manager
                    ),
                    "hook_manager": (
                        run_context.hook_manager
                        if run_context is not None
                        else None
                    ),
                    "rollout_budget": metadata.get("_rollout_budget"),
                    "deadline_monotonic": getattr(context, "deadline_monotonic", None),
                    # Server-initiated MCP requests inherit the exact tool-call
                    # owner.  Keeping the cancellation object here lets sampling
                    # and elicitation stop with the originating turn instead of
                    # outliving a cancelled tool call in the shared MCP client.
                    "cancel_event": getattr(context, "cancel_event", None),
                }
            result: MCPCallResult = await client.call_tool(
                self._tool_def.name,
                args,
                request_owner=request_owner,
            )
        except Exception as exc:
            return self._error_result(f"MCP tool '{self._tool_def.name}' failed: {exc}")

        result_text = result.summary_text
        if result.is_error:
            return self._error_result(result_text or "MCP tool execution failed")

        images: list[dict[str, str]] = []
        resource_text: list[str] = []
        output_files: list[dict[str, Any]] = []
        for block in result.content:
            if not isinstance(block, dict):
                continue
            block_type = str(block.get("type") or "").strip().lower()
            if block_type == "image":
                data = str(block.get("data") or "").strip()
                if data:
                    images.append({
                        "media_type": str(
                            block.get("mimeType")
                            or block.get("media_type")
                            or "image/png"
                        ),
                        "data": data,
                    })
                continue
            if block_type != "resource":
                continue
            resource = block.get("resource")
            if not isinstance(resource, dict):
                continue
            uri = str(resource.get("uri") or "resource").strip() or "resource"
            media_type = str(
                resource.get("mimeType") or resource.get("media_type") or ""
            ).strip()
            text_content = resource.get("text")
            if isinstance(text_content, str):
                resource_text.append(f"[resource: {uri}]\n{text_content}")
                continue
            blob = resource.get("blob")
            if isinstance(blob, str) and self._artifact_store is not None:
                artifact_id = self._artifact_store.save(
                    blob,
                    source=f"mcp:{self._server_name}:{uri}",
                    type="mcp_resource",
                    media_type=media_type,
                )
                output_files.append({
                    "artifact_id": artifact_id,
                    "uri": uri,
                    "media_type": media_type,
                    "encoding": "base64",
                })

        content_parts = [part for part in (result.text.strip(), *resource_text) if part]
        if output_files:
            content_parts.extend(
                f"[resource artifact: {item['uri']} -> {item['artifact_id']}]"
                for item in output_files
            )
        typed_content = "\n\n".join(content_parts) or result_text

        # Result sizing and artifact promotion belong to the shared tool
        # execution path (Pi's 2,000-line/50-KiB contract). Keeping an MCP-only
        # character threshold here caused a second, undocumented truncation
        # policy and made the same tool behave differently through MCP.
        return ToolResult(
            content=typed_content,
            images=images,
            output_files=output_files,
            status="success",
        )


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
        self._wire_name_owner: dict[str, str] = {}
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
            existing_owner = self._wire_name_owner.get(proxy.name)
            if existing_owner is not None:
                # Manual/user MCP config outranks plugin config. For equal
                # scopes, preserve deterministic first-wins behavior rather
                # than silently routing a wire name to a different tool.
                replace_plugin = existing_owner.startswith("plugin:") and not server_name.startswith("plugin:")
                if not replace_plugin:
                    logger.warning(
                        "[MCPRegistry] Skipped normalized tool-name collision %s from %s; owned by %s",
                        proxy.name,
                        server_name,
                        existing_owner,
                    )
                    continue
                previous_names = self._server_tools.get(existing_owner, [])
                self._server_tools[existing_owner] = [
                    name for name in previous_names if name != proxy.name
                ]
                self._tool_registry.unregister(proxy.name)
                self._wire_name_owner.pop(proxy.name, None)
                logger.info(
                    "[MCPRegistry] Manual server %s replaced plugin wire-name owner %s for %s",
                    server_name,
                    existing_owner,
                    proxy.name,
                )
            elif self._tool_registry.has_tool(proxy.name):
                logger.warning(
                    "[MCPRegistry] Skipped MCP tool %s from %s; a non-MCP tool already owns the name",
                    proxy.name,
                    server_name,
                )
                continue
            self._tool_registry.register(proxy, owner=f"mcp:{server_name}")
            self._wire_name_owner[proxy.name] = server_name
            registered_names.append(proxy.name)
            logger.info("[MCPRegistry] Registered tool: %s (from %s)", proxy.name, server_name)

        self._server_tools[server_name] = registered_names
        return len(registered_names)

    def unregister_server_tools(self, server_name: str) -> None:
        """Unregister all tools that came from one MCP server."""
        tool_names = self._server_tools.pop(server_name, [])
        for name in tool_names:
            if self._wire_name_owner.get(name) != server_name:
                continue
            self._tool_registry.unregister(name)
            self._wire_name_owner.pop(name, None)
            logger.info("[MCPRegistry] Unregistered tool: %s", name)

    def get_server_tool_count(self, server_name: str) -> int:
        """Return the number of tools registered for one server."""
        return len(self._server_tools.get(server_name, []))

    def get_all_mcp_tools(self) -> dict[str, list[str]]:
        """Return the current server-to-tool mapping."""
        return dict(self._server_tools)
