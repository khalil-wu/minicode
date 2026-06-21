from __future__ import annotations

import logging
from typing import Any

from backend.tools.base import BaseTool, ToolResult, ToolSchema

logger = logging.getLogger(__name__)


class ListMcpResourcesTool(BaseTool):
    """
    Discover available MCP (Model Context Protocol) resources from connected servers.

    MCP resources are dynamic data sources — database schemas, API documentation,
    configuration state, knowledge bases — exposed by connected MCP servers. This
    tool returns a catalog of resource URIs, names, and metadata that can then be
    fetched individually with read_mcp_resource.
    """

    read_only = True

    def __init__(self, mcp_manager: Any | None) -> None:
        self.name = "list_mcp_resources"
        self.description = (
            "List all available resources from every connected MCP server. "
            "Use this FIRST when you need external context that MCP servers might provide — "
            "database schemas, API docs, live configuration, or any dynamic data source exposed "
            "via MCP. Returns a catalog of resource URIs with names and mime types. "
            "Do NOT use this when the needed information is already in the conversation, can be "
            "found via read_file in the workspace, or is general knowledge from training data. "
            "After listing, use read_mcp_resource with a specific URI to fetch the actual content."
        )
        self._mcp_manager = mcp_manager

    def get_spec(self):
        from backend.tools.contracts import ToolSpec

        return ToolSpec(
            name=self.name,
            capability="mcp.discover",
            accepted_resource_types=("mcp_server",),
            empty_args_policy="allow",
        )

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
        for name, client in self._mcp_manager.iter_connected_clients():
            try:
                resources = await client.list_resources()
                for r in resources:
                    all_resources.append(f"- URI: {r.uri} | Name: {r.name} | MimeType: {r.mime_type} | Server: {name}")
            except Exception as e:
                logger.warning(f"Error fetching resources from {name}: {e}")

        if not all_resources:
            return self._success_result("No MCP resources are currently available.")

        return self._success_result("Available MCP Resources:\n" + "\n".join(all_resources))


class ReadMcpResourceTool(BaseTool):
    """
    Fetch the contents of a specific MCP resource by its URI.

    Reads data from a resource previously discovered via list_mcp_resources.
    The fetched content is injected into the conversation for the model to
    reference. Large resources are stored as artifacts with a preview returned
    inline.
    """

    read_only = True

    def __init__(self, mcp_manager: Any | None, artifact_store: Any | None = None) -> None:
        self.name = "read_mcp_resource"
        self.description = (
            "Read a specific resource from a connected MCP server by its exact URI. "
            "Use AFTER list_mcp_resources has shown available resource URIs — do not guess URIs. "
            "When list_mcp_resources includes a Server value for the resource, pass that exact "
            "server name to avoid reading a same-URI resource from a different MCP server. "
            "The fetched content is injected into the conversation as contextual data for reasoning. "
            "Large resources are automatically stored as artifacts with a preview returned inline; "
            "use read_artifact if the full content is needed. "
            "Do NOT use this for workspace files (use read_file instead) or web content (use web_fetch). "
            "If the resource read fails, try a different URI from the list_mcp_resources output."
        )
        self._mcp_manager = mcp_manager
        self._artifact_store = artifact_store

    def get_spec(self):
        from backend.tools.contracts import ToolSpec

        return ToolSpec(
            name=self.name,
            capability="mcp.read",
            required_args=("uri",),
            arg_roles={"uri": "mcp_resource_uri", "server": "mcp_server"},
            arg_sources={"uri": ("mcp_resource_list",)},
            repair_policy={"uri": "resource_resolver"},
            accepted_resource_types=("mcp_resource",),
            empty_args_policy="repair_or_block",
            blocked_guidance=(
                "missing required uri. Call list_mcp_resources first to discover available "
                "resource URIs, then retry read_mcp_resource with a specific URI from the result."
            ),
        )

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "uri": {
                        "type": "string",
                        "description": "The exact URI of the MCP resource to read, as returned by list_mcp_resources."
                    },
                    "server": {
                        "type": "string",
                        "description": (
                            "Optional MCP server name from list_mcp_resources. "
                            "Use this when the listing includes a Server value or when multiple servers may expose the same URI."
                        )
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
        server_name = str(args.get("server") or "").strip()

        found_content = None
        clients = self._mcp_manager.iter_connected_clients()
        if server_name:
            clients = [
                (name, client)
                for name, client in clients
                if name == server_name
            ]
            if not clients:
                return self._error_result(f"MCP server is not connected: {server_name}")

        for _name, client in clients:
            try:
                # Ignore invalid URI exceptions that typical MCP servers might throw
                content = await client.read_resource(uri)
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
