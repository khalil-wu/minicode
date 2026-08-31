import asyncio

from backend.services.tool_registry_factory import build_tool_registry as _build_tool_registry
from backend.artifact.store import ArtifactStore
from backend.mcp.client import MCPAuthenticationError, MCPClient, MCPServerCapabilities
from backend.mcp.manager import ServerStatus, classify_mcp_phase
from backend.tools.mcp_tools import (
    ListMcpResourceNotificationsTool,
    ListMcpResourceTemplatesTool,
    SubscribeMcpResourceTool,
    UnsubscribeMcpResourceTool,
)


class _ResourceClient(MCPClient):
    def __init__(self) -> None:
        super().__init__(server_name="docs")
        self._connected = True
        self._server_capabilities = MCPServerCapabilities(
            resources=True,
            resources_subscribe=True,
            resources_list_changed=True,
        )
        self.calls: list[tuple[str, dict | None]] = []

    async def _request(self, method, params=None):
        self.calls.append((method, params))
        if method == "resources/templates/list":
            return {
                "resourceTemplates": [
                    {
                        "uriTemplate": "docs://{package}/{version}",
                        "name": "Package docs",
                        "description": "Versioned package docs",
                        "mimeType": "text/markdown",
                    }
                ]
            }
        if method in {"resources/subscribe", "resources/unsubscribe"}:
            return {}
        return None


class _ResourceManager:
    def __init__(self) -> None:
        self.client = _ResourceClient()

    def iter_connected_clients(self):
        return [("docs", self.client)]

    def get_client(self, name):
        return self.client if name == "docs" else None

    def get_all_tools(self):
        return {}


def test_mcp_client_lists_resource_templates_and_tracks_subscriptions() -> None:
    async def run() -> None:
        client = _ResourceClient()

        templates = await client.list_resource_templates()
        subscribed = await client.subscribe_resource("docs://mini/latest")
        unsubscribed = await client.unsubscribe_resource("docs://mini/latest")

        assert templates[0].uri_template == "docs://{package}/{version}"
        assert templates[0].mime_type == "text/markdown"
        assert subscribed is True
        assert unsubscribed is True
        assert client.list_resource_subscriptions() == []
        assert ("resources/subscribe", {"uri": "docs://mini/latest"}) in client.calls

    asyncio.run(run())


def test_mcp_resource_notification_buffer_is_consumed_once() -> None:
    client = _ResourceClient()

    client._record_resource_notification(
        "notifications/resources/updated",
        {"uri": "docs://mini/latest"},
    )

    first = client.consume_resource_notifications()
    second = client.consume_resource_notifications()

    assert first == [
        {
            "method": "notifications/resources/updated",
            "uri": "docs://mini/latest",
            "params": {"uri": "docs://mini/latest"},
        }
    ]
    assert second == []


def test_mcp_client_disconnect_callback_fires_immediately() -> None:
    async def run() -> None:
        disconnected: list[str] = []

        async def on_disconnect(name: str) -> None:
            disconnected.append(name)

        client = MCPClient(server_name="docs", on_disconnect=on_disconnect)
        client._loop = asyncio.get_running_loop()
        client._connected = True

        client._mark_disconnected()
        await asyncio.sleep(0)

        assert disconnected == ["docs"]

    asyncio.run(run())


def test_mcp_client_responds_to_roots_list_request(tmp_path) -> None:
    async def run() -> None:
        # Roots are owned by the client instance: with no explicit
        # ``workspace_root`` the client deliberately fails closed rather than
        # consulting process-global workspace state (which could leak another
        # conversation's tree into this MCP session). Only an explicitly
        # scoped client advertises a root.
        unscoped = MCPClient(server_name="roots")
        unscoped_result = await unscoped._sdk_list_roots(None)
        assert unscoped_result.model_dump(by_alias=True, mode="json")["roots"] == []

        client = MCPClient(server_name="roots", workspace_root=tmp_path)
        result = await client._sdk_list_roots(None)
        roots = result.model_dump(by_alias=True, mode="json")["roots"]
        assert roots
        assert all(root["uri"].startswith("file:///") for root in roots)
        assert [root["uri"] for root in roots] == [tmp_path.resolve().as_uri()]

    asyncio.run(run())


def test_mcp_client_reports_unsupported_elicitation_request() -> None:
    async def run() -> None:
        client = MCPClient(server_name="roots")
        result = await client._sdk_elicitation_callback(None, {})

        assert result.code == -32600
        assert result.message == "elicitation not supported"

    asyncio.run(run())


def test_mcp_resource_template_and_subscription_tools() -> None:
    async def run() -> None:
        manager = _ResourceManager()

        listed = await ListMcpResourceTemplatesTool(manager).execute({})
        subscribed = await SubscribeMcpResourceTool(manager).execute({
            "server": "docs",
            "uri": "docs://mini/latest",
        })
        notifications = await ListMcpResourceNotificationsTool(manager).execute({})
        unsubscribed = await UnsubscribeMcpResourceTool(manager).execute({
            "server": "docs",
            "uri": "docs://mini/latest",
        })

        assert "Template: docs://{package}/{version}" in listed.content
        assert "Subscribed to MCP resource updates" in subscribed.content
        assert "Subscribed: docs://mini/latest" in notifications.content
        assert "Unsubscribed from MCP resource updates" in unsubscribed.content

    asyncio.run(run())


def test_default_registry_registers_mcp_resource_depth_bridges(tmp_path) -> None:
    registry = _build_tool_registry(
        ArtifactStore(storage_dir=tmp_path),
        mcp_manager=_ResourceManager(),
    )
    tool_names = set(registry.list_tools())
    summary = registry.build_capability_summary()

    assert {
        "list_mcp_resource_templates",
        "subscribe_mcp_resource",
        "unsubscribe_mcp_resource",
        "list_mcp_resource_notifications",
    } <= tool_names
    assert summary["mcp_resource_template_bridge"] is True
    assert summary["mcp_resource_subscription_bridge"] is True


def test_mcp_authentication_error_uses_structured_phase() -> None:
    phase = classify_mcp_phase(
        ServerStatus.ERROR,
        "gateway rejected request",
        MCPAuthenticationError("authorization required"),
    )

    assert phase == ("auth_required", False, True)
