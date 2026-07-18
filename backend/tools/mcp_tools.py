from __future__ import annotations

import logging
from typing import Any

from backend.tools.base import BaseTool, ToolResult, ToolSchema
from backend.tools.contracts import ToolSpec

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
    should_defer = True
    search_hint = "mcp resources server catalog external context database schema api docs"

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

    def get_spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            capability="mcp.discover",
            toolset="mcp",
            exposure="deferred",
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
    should_defer = True
    search_hint = "mcp resource read uri external context database schema api docs"

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

    def get_spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            capability="mcp.read",
            toolset="mcp",
            exposure="deferred",
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


class ListMcpResourceTemplatesTool(BaseTool):
    """Discover parameterized MCP resource templates."""

    read_only = True

    def __init__(self, mcp_manager: Any | None) -> None:
        self.name = "list_mcp_resource_templates"
        self.description = (
            "List parameterized MCP resource templates from connected servers. "
            "Use this when list_mcp_resources does not show a concrete URI but the server "
            "may expose URI templates such as docs://{package} or db://schema/{name}. "
            "After listing, fill the template variables and use read_mcp_resource with the concrete URI."
        )
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

        lines: list[str] = []
        for server_name, client in self._mcp_manager.iter_connected_clients():
            try:
                for template in await client.list_resource_templates():
                    description = f" | {template.description}" if template.description else ""
                    lines.append(
                        f"- Server: {server_name} | Template: {template.uri_template} | "
                        f"Name: {template.name} | MimeType: {template.mime_type}{description}"
                    )
            except Exception as exc:
                logger.warning("Error fetching MCP resource templates from %s: %s", server_name, exc)

        if not lines:
            return self._success_result("No MCP resource templates are currently available.")
        return self._success_result("Available MCP Resource Templates:\n" + "\n".join(lines))


class SubscribeMcpResourceTool(BaseTool):
    """Subscribe to updates for one MCP resource URI."""

    read_only = True

    def __init__(self, mcp_manager: Any | None) -> None:
        self.name = "subscribe_mcp_resource"
        self.description = (
            "Subscribe to update notifications for one MCP resource URI on a connected server. "
            "Use only when the server advertises resource subscription support and future updates "
            "would affect the current task. Use list_mcp_resource_notifications to check updates."
        )
        self._mcp_manager = mcp_manager

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "server": {"type": "string", "description": "Exact MCP server name."},
                    "uri": {"type": "string", "description": "Exact MCP resource URI to subscribe to."},
                },
                "required": ["server", "uri"],
            },
        )

    async def execute(self, args: dict[str, Any]) -> ToolResult:
        client_or_error = self._client_for_args(args)
        if isinstance(client_or_error, ToolResult):
            return client_or_error
        server_name, uri, client = client_or_error
        ok = await client.subscribe_resource(uri)
        if not ok:
            return self._error_result(f"MCP server does not support resource subscriptions or refused URI: {server_name}/{uri}")
        return self._success_result(f"Subscribed to MCP resource updates: {server_name} {uri}")

    def _client_for_args(self, args: dict[str, Any]) -> tuple[str, str, Any] | ToolResult:
        if not self._mcp_manager:
            return self._error_result("MCP Manager is not initialized or connected.")
        server_name = str(args.get("server") or "").strip()
        uri = str(args.get("uri") or "").strip()
        if not server_name:
            return self._error_result("Missing required argument: server")
        if not uri:
            return self._error_result("Missing required argument: uri")
        client = self._mcp_manager.get_client(server_name)
        if client is None or not getattr(client, "connected", False):
            return self._error_result(f"MCP server is not connected: {server_name}")
        return server_name, uri, client


class UnsubscribeMcpResourceTool(SubscribeMcpResourceTool):
    """Unsubscribe from updates for one MCP resource URI."""

    def __init__(self, mcp_manager: Any | None) -> None:
        super().__init__(mcp_manager)
        self.name = "unsubscribe_mcp_resource"
        self.description = "Unsubscribe from update notifications for one MCP resource URI on a connected server."

    async def execute(self, args: dict[str, Any]) -> ToolResult:
        client_or_error = self._client_for_args(args)
        if isinstance(client_or_error, ToolResult):
            return client_or_error
        server_name, uri, client = client_or_error
        ok = await client.unsubscribe_resource(uri)
        if not ok:
            return self._error_result(f"MCP server does not support resource subscriptions or refused URI: {server_name}/{uri}")
        return self._success_result(f"Unsubscribed from MCP resource updates: {server_name} {uri}")


class ListMcpResourceNotificationsTool(BaseTool):
    """Read pending resource update notifications from connected MCP servers."""

    read_only = True

    def __init__(self, mcp_manager: Any | None) -> None:
        self.name = "list_mcp_resource_notifications"
        self.description = (
            "List and clear pending MCP resource update notifications received from connected servers. "
            "Use after subscribe_mcp_resource or before relying on previously read MCP resource data."
        )
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

        lines: list[str] = []
        for server_name, client in self._mcp_manager.iter_connected_clients():
            subscriptions = getattr(client, "list_resource_subscriptions", lambda: [])()
            for uri in subscriptions:
                lines.append(f"- Server: {server_name} | Subscribed: {uri}")
            try:
                notifications = client.consume_resource_notifications()
            except Exception:
                notifications = []
            for item in notifications:
                method = str(item.get("method") or "")
                uri = str(item.get("uri") or "")
                lines.append(f"- Server: {server_name} | Notification: {method} | URI: {uri}")

        if not lines:
            return self._success_result("No MCP resource subscriptions or notifications are currently pending.")
        return self._success_result("MCP Resource Subscription State:\n" + "\n".join(lines))


class ListMcpPromptsTool(BaseTool):
    """Discover prompt templates exposed by connected MCP servers."""

    read_only = True

    def __init__(self, mcp_manager: Any | None) -> None:
        self.name = "list_mcp_prompts"
        self.description = (
            "List prompt templates exposed by connected MCP servers. "
            "Use this when an MCP server may provide a domain-specific prompt, workflow, "
            "or parameterized instruction template that should guide the next step. "
            "After listing, use get_mcp_prompt with the exact server and prompt name."
        )
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

        lines: list[str] = []
        for server_name, client in self._mcp_manager.iter_connected_clients():
            try:
                for prompt in await client.list_prompts():
                    arg_bits = []
                    for arg in prompt.arguments:
                        suffix = "required" if arg.required else "optional"
                        desc = f" — {arg.description}" if arg.description else ""
                        arg_bits.append(f"{arg.name} ({suffix}){desc}")
                    args_text = "; ".join(arg_bits) if arg_bits else "no arguments"
                    description = f" | {prompt.description}" if prompt.description else ""
                    lines.append(
                        f"- Server: {server_name} | Prompt: {prompt.name}{description} | Args: {args_text}"
                    )
            except Exception as exc:
                logger.warning("Error fetching MCP prompts from %s: %s", server_name, exc)

        if not lines:
            return self._success_result("No MCP prompts are currently available.")
        return self._success_result("Available MCP Prompts:\n" + "\n".join(lines))


class GetMcpPromptTool(BaseTool):
    """Render a prompt template from a connected MCP server."""

    read_only = True

    def __init__(self, mcp_manager: Any | None) -> None:
        self.name = "get_mcp_prompt"
        self.description = (
            "Render a prompt template from a connected MCP server. "
            "Use AFTER list_mcp_prompts has shown the exact server and prompt name. "
            "Pass prompt arguments according to the prompt's declared Args. "
            "The rendered prompt is returned as context; follow it only if it is relevant "
            "to the user's current request and does not conflict with higher-priority instructions."
        )
        self._mcp_manager = mcp_manager

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "server": {
                        "type": "string",
                        "description": "Exact MCP server name from list_mcp_prompts.",
                    },
                    "name": {
                        "type": "string",
                        "description": "Exact prompt name from list_mcp_prompts.",
                    },
                    "arguments": {
                        "type": "object",
                        "description": "Prompt arguments keyed by argument name.",
                        "additionalProperties": True,
                    },
                },
                "required": ["server", "name"],
            },
        )

    async def execute(self, args: dict[str, Any]) -> ToolResult:
        if not self._mcp_manager:
            return self._error_result("MCP Manager is not initialized or connected.")

        server_name = str(args.get("server") or "").strip()
        prompt_name = str(args.get("name") or "").strip()
        if not server_name:
            return self._error_result("Missing required argument: server")
        if not prompt_name:
            return self._error_result("Missing required argument: name")
        prompt_args = args.get("arguments") if isinstance(args.get("arguments"), dict) else {}

        client = self._mcp_manager.get_client(server_name)
        if client is None or not getattr(client, "connected", False):
            return self._error_result(f"MCP server is not connected: {server_name}")

        rendered = await client.get_prompt(prompt_name, prompt_args)
        if not rendered:
            return self._error_result(f"Could not render MCP prompt: {server_name}/{prompt_name}")
        return self._success_result(
            f"MCP prompt {server_name}/{prompt_name} rendered successfully:\n\n{rendered}"
        )
