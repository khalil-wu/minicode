from __future__ import annotations

import logging
from typing import Any

from backend.tools.base import BaseTool, ToolResult, ToolSchema

logger = logging.getLogger(__name__)

class ListMcpResourcesTool(BaseTool):
    """
    List available MCP (Model Context Protocol) resources.
    MCP resources are dynamic data sources (like database schemas, external API endpoints, internal state) exposed by connected servers.
    """
    read_only = True

    def __init__(self, mcp_manager: Any | None) -> None:
        self.name = "list_mcp_resources"
        self.description = "List all available resources exposed by connected MCP servers. Returns a list of URIs and Resource names."
        self._mcp_manager = mcp_manager

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={"type": "object", "properties": {}},
        )

    async def execute(self, args: dict[str, Any]) -> ToolResult:
        if not self._mcp_manager:
            return self._error_result("MCP Manager is not initialized or connected.")

        all_resources = []
        for name, state in self._mcp_manager._servers.items():
            if state.client and state.client.connected:
                try:
                    resources = await state.client.list_resources()
                    for r in resources:
                        all_resources.append(f"- URI: {r.uri} | Name: {r.name} | MimeType: {r.mime_type} | Server: {name}")
                except Exception as e:
                    logger.warning(f"Error fetching resources from {name}: {e}")

        if not all_resources:
            return self._success_result("No MCP resources are currently available.")

        return self._success_result("Available MCP Resources:\n" + "\n".join(all_resources))


class ReadMcpResourceTool(BaseTool):
    """
    Read the contents of a specific MCP resource by its URI.
    """
    read_only = True
    def __init__(self, mcp_manager: Any | None, artifact_store: Any | None = None) -> None:
        self.name = "read_mcp_resource"
        self.description = "Read the contents of a specific MCP resource by its URI. Provide the Exact URI returned from list_mcp_resources."
        self._mcp_manager = mcp_manager
        self._artifact_store = artifact_store

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "uri": {
                        "type": "string",
                        "description": "The exact URI of the MCP resource to read."
                    }
                },
                "required": ["uri"]
            },
        )

    async def execute(self, args: dict[str, Any]) -> ToolResult:
        if not self._mcp_manager:
            return self._error_result("MCP Manager is not initialized or connected.")

        uri = args.get("uri")
        if not uri:
            return self._error_result("Missing required argument: uri")

        found_content = None
        for name, state in self._mcp_manager._servers.items():
            if state.client and state.client.connected:
                try:
                    # Ignore invalid URI exceptions that typical MCP servers might throw
                    content = await state.client.read_resource(uri)
                    if content:
                        found_content = content
                        break
                except Exception:
                    continue

        if not found_content:
            return self._error_result(f"Could not find or read MCP resource for URI: {uri}")

        if self._artifact_store and len(found_content) > 2000:
            artifact_id = self._artifact_store.save(
                content=found_content,
                source=f"MCP Resource {uri}",
                type="mcp_resource",
            )
            preview = "\n".join(found_content.split("\n")[:10])
            return self._success_result(
                content=f"Resource {uri} read successfully.\nContent saved as Artifact ({len(found_content)} chars).\nPreview:\n{preview}...",
                artifact_id=artifact_id,
                artifact_preview=preview
            )

        return self._success_result(found_content)
