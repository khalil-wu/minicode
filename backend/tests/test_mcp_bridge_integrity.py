from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from backend.artifact.store import ArtifactStore
from backend.agent.context import ContextBuilder
from backend.agent.state import AgentState
from backend.agent.tool_execution import store_result_events
from backend.llm.base import ToolCallEvent
from backend.mcp.client import MCPCallResult, MCPToolDef
from backend.mcp.registry import MCPToolProxy, MCPToolRegistry
from backend.tools.mcp_tools import (
    ListMcpPromptsTool,
    ListMcpResourceNotificationsTool,
    ListMcpResourcesTool,
    ListMcpResourceTemplatesTool,
    ReadMcpResourceTool,
)
from backend.tools.swarm_tools import SendMessageTool
from backend.tools.registry import ToolRegistry
from backend.services.chat_api_service import generated_artifact_native_payload, ChatApiServiceError
from backend.permissions.context import PermissionContext, ToolExecutionContext


class _CatalogClient:
    def __init__(self, entry: SimpleNamespace | None = None, *, fail: bool = False) -> None:
        self.entry = entry
        self.fail = fail

    async def _list(self) -> list[SimpleNamespace]:
        if self.fail:
            raise RuntimeError("catalog unavailable")
        return [self.entry] if self.entry is not None else []

    async def list_resources(self) -> list[SimpleNamespace]:
        return await self._list()

    async def list_resources_page(self, cursor=None):
        assert cursor is None
        return await self._list(), None

    async def list_resource_templates(self) -> list[SimpleNamespace]:
        return await self._list()

    async def list_resource_templates_page(self, cursor=None):
        assert cursor is None
        return await self._list(), None

    async def list_prompts(self) -> list[SimpleNamespace]:
        return await self._list()


class _Manager:
    def __init__(self, clients: list[tuple[str, object]]) -> None:
        self.clients = clients

    def iter_connected_clients(self) -> list[tuple[str, object]]:
        return self.clients

    def get_client(self, server_name: str):
        return next((client for name, client in self.clients if name == server_name), None)


@pytest.mark.parametrize(
    "tool_type, entry, expected",
    [
        (
            ListMcpResourcesTool,
            SimpleNamespace(uri="docs://item", name="Item", mime_type="text/plain"),
            "docs://item",
        ),
        (
            ListMcpResourceTemplatesTool,
            SimpleNamespace(
                uri_template="docs://{name}", name="Template", mime_type="text/plain", description=""
            ),
            "docs://{name}",
        ),
        (
            ListMcpPromptsTool,
            SimpleNamespace(name="review", description="", arguments=[]),
            "Prompt: review",
        ),
    ],
)
def test_mcp_catalog_listing_reports_server_failures(tool_type, entry, expected) -> None:
    failed = _CatalogClient(fail=True)
    only_failure = asyncio.run(tool_type(_Manager([("broken", failed)])).execute({}))
    assert only_failure.is_error
    assert "broken: RuntimeError: catalog unavailable" in only_failure.content

    partial = asyncio.run(
        tool_type(_Manager([("working", _CatalogClient(entry)), ("broken", failed)])).execute({})
    )
    assert not partial.is_error
    assert partial.status == "partial"
    assert expected in partial.content
    assert "Unavailable servers: broken: RuntimeError: catalog unavailable" in partial.content


def test_empty_mcp_resource_is_a_successful_read() -> None:
    class _EmptyResourceClient:
        async def read_resource(self, uri: str) -> list[dict[str, str]]:
            assert uri == "docs://empty"
            return [{"uri": uri, "text": ""}]

    result = asyncio.run(
        ReadMcpResourceTool(_Manager([("docs", _EmptyResourceClient())])).execute(
            {"uri": "docs://empty", "server": "docs"}
        )
    )
    assert not result.is_error
    assert result.content == "Resource docs/docs://empty is empty."


def test_mcp_resource_read_requires_server_and_keeps_binary_content(tmp_path) -> None:
    class _ResourceClient:
        def __init__(self, text: str) -> None:
            self.text = text
            self.calls = 0

        async def read_resource(self, uri: str) -> list[dict[str, str]]:
            self.calls += 1
            return [
                {"uri": uri, "text": self.text},
                {"uri": uri, "blob": "AQID", "mimeType": "application/octet-stream"},
            ]

    first = _ResourceClient("wrong server")
    second = _ResourceClient("right server")
    tool = ReadMcpResourceTool(
        _Manager([("first", first), ("second", second)]),
        ArtifactStore(storage_dir=tmp_path),
    )
    missing_server = asyncio.run(tool.execute({"uri": "docs://same"}))
    selected = asyncio.run(tool.execute({"server": "second", "uri": "docs://same"}))

    assert tool.get_schema().parameters["required"] == ["server", "uri"]
    assert missing_server.is_error
    assert first.calls == 0
    assert second.calls == 1
    assert "right server" in selected.content
    assert "wrong server" not in selected.content
    assert selected.output_files == []
    assert selected.artifact_id
    assert selected.artifact_kind == "binary"
    assert selected.artifact_media_type == "application/octet-stream"
    assert selected.artifact_bytes == 3


def test_mcp_ui_only_tool_is_not_registered_for_model() -> None:
    registry = ToolRegistry()
    mcp_registry = MCPToolRegistry(registry)
    tools = [
        MCPToolDef(name="ui_only", description="UI only", input_schema={"type": "object", "properties": {}}, meta={"ui": {"visibility": ["app"]}}),
        MCPToolDef(name="model_tool", description="Model tool", input_schema={"type": "object", "properties": {}}, meta={"ui": {"visibility": ["model"]}}),
        MCPToolDef(name="ordinary", description="Ordinary", input_schema={"type": "object", "properties": {}}),
    ]
    assert mcp_registry.register_server_tools("demo", tools, SimpleNamespace(connected=True)) == 2
    assert registry.get_tool("mcp__demo__ui_only") is None
    assert registry.get_tool("mcp__demo__model_tool") is not None
    assert registry.get_tool("mcp__demo__ordinary") is not None


def test_mcp_proxy_keeps_resource_link_and_binary_artifact(tmp_path) -> None:
    class _Client:
        connected = True

        async def call_tool(self, _name, _args, *, request_owner=None):
            return MCPCallResult(content=[
                {"type": "resource_link", "uri": "docs://next", "name": "Next page"},
                {"type": "resource", "resource": {"uri": "docs://image", "blob": "AQID", "mimeType": "image/png"}},
            ])

    proxy = MCPToolProxy("docs", MCPToolDef(name="lookup", description="Lookup"), _Client(), ArtifactStore(storage_dir=tmp_path))
    result = asyncio.run(proxy.execute({}))
    assert "docs://next" in result.content
    assert result.artifact_id and result.artifact_media_type == "image/png"
    assert result.output_files == []
    registry = ToolRegistry()
    registry.register(proxy)
    events = store_result_events(
        ToolCallEvent(id="lookup", name=proxy.name, arguments={}), result,
        ContextBuilder(), AgentState(user_message="Read the resource"),
        tool_ctx=ToolExecutionContext(
            permission=PermissionContext(mode="bypass"), conversation_id="owner",
            metadata={"assistant_message_id": "answer"},
        ), tool_registry=registry,
    )
    assert [event.type for event in events] == ["artifact.preview", "tool_result"]
    assert events[0].data["artifact_id"] == result.artifact_id
    assert events[0].data["media_type"] == "image/png"


def test_mcp_binary_artifact_raw_response_preserves_bytes_and_owner(tmp_path) -> None:
    store = ArtifactStore(storage_dir=tmp_path / "artifacts")
    artifact_id = store.save("AQID", source="mcp:docs:docs://data", type="mcp_resource",
                             media_type="application/octet-stream", conversation_id="owner", workspace_root=tmp_path)
    session = SimpleNamespace(artifact_store=store,
        conversation_repo=SimpleNamespace(get_conversation=lambda value: SimpleNamespace(id=value, archived=False)),
        session_lifecycle=SimpleNamespace(workspace_root_for_conversation=lambda _: str(tmp_path)))
    manager = SimpleNamespace(get_session=lambda _: session)
    body, media_type, name = generated_artifact_native_payload(
        session_id="session", conversation_id="owner", artifact_id=artifact_id, ws_manager=manager,
    )
    assert body == b"\x01\x02\x03"
    assert media_type == "application/octet-stream"
    assert name.endswith(".bin")
    with pytest.raises(ChatApiServiceError) as denied:
        generated_artifact_native_payload(
            session_id="session", conversation_id="other", artifact_id=artifact_id, ws_manager=manager,
        )
    assert denied.value.status_code == 404


def test_mcp_resource_listing_exposes_server_cursor() -> None:
    class _PagedClient:
        async def list_resources_page(self, cursor=None):
            if cursor is None:
                return [SimpleNamespace(uri="docs://first", name="First", mime_type="text/plain")], "page-2"
            assert cursor == "page-2"
            return [SimpleNamespace(uri="docs://second", name="Second", mime_type="text/plain")], None

    tool = ListMcpResourcesTool(_Manager([("docs", _PagedClient())]))
    first = asyncio.run(tool.execute({}))
    second = asyncio.run(tool.execute({"server": "docs", "cursor": "page-2"}))
    assert "Next cursor: page-2 | Server: docs" in first.content
    assert "docs://second" in second.content
    assert "Next cursor" not in second.content


def test_mcp_notification_read_reports_a_failed_queue() -> None:
    class _NotificationClient:
        def __init__(self, *, fail: bool = False) -> None:
            self.fail = fail

        def list_resource_subscriptions(self) -> list[str]:
            return []

        def consume_resource_notifications(self) -> list[dict[str, str]]:
            if self.fail:
                raise RuntimeError("queue unavailable")
            return [{"method": "resources/updated", "uri": "docs://item"}]

    only_failure = asyncio.run(
        ListMcpResourceNotificationsTool(
            _Manager([("broken", _NotificationClient(fail=True))])
        ).execute({})
    )
    assert only_failure.is_error
    assert "broken: RuntimeError: queue unavailable" in only_failure.content

    partial = asyncio.run(
        ListMcpResourceNotificationsTool(
            _Manager([("docs", _NotificationClient()), ("broken", _NotificationClient(fail=True))])
        ).execute({})
    )
    assert partial.status == "partial"
    assert "docs://item" in partial.content
    assert "Unavailable servers: broken: RuntimeError: queue unavailable" in partial.content


def test_mailbox_send_keeps_read_only_delegation_without_parallel_reordering() -> None:
    tool = SendMessageTool()
    assert tool.is_read_only({"recipient": "parent", "message": "report"})
    assert not tool.is_idempotent({"recipient": "parent", "message": "report"})
    assert not tool.is_concurrency_safe({"recipient": "parent", "message": "report"})


def test_mcp_proxy_exposes_its_effective_read_only_contract() -> None:
    proxy = MCPToolProxy(
        "desktop",
        MCPToolDef(
            name="inspect",
            description="Inspect selected context",
            annotations={"readOnlyHint": True, "openWorldHint": True},
        ),
        None,
    )
    assert not proxy.is_read_only({})
    assert proxy.to_runtime_metadata()["read_only"] is False
