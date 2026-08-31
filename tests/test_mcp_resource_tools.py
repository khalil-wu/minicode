import asyncio
from types import SimpleNamespace

from backend.artifact.store import ArtifactStore
from backend.mcp.manager import MCPServerConfig, MCPServerManager, MCPServerState, ServerStatus
from backend.tools.mcp_tools import (
    GetMcpPromptTool,
    ListMcpPromptsTool,
    ListMcpResourceNotificationsTool,
    ListMcpResourcesTool,
    ListMcpResourceTemplatesTool,
    ReadMcpResourceTool,
    SubscribeMcpResourceTool,
    UnsubscribeMcpResourceTool,
)


class _FakeMcpClient:
    def __init__(
        self,
        *,
        resources: list[SimpleNamespace] | None = None,
        content_by_uri: dict[str, str] | None = None,
        connected: bool = True,
    ) -> None:
        self.connected = connected
        self._resources = resources or []
        self._content_by_uri = content_by_uri or {}
        self.list_calls = 0
        self.read_calls: list[str] = []

    async def list_resources(self) -> list[SimpleNamespace]:
        self.list_calls += 1
        return self._resources

    async def read_resource(self, uri: str) -> str:
        self.read_calls.append(uri)
        if uri not in self._content_by_uri:
            raise RuntimeError("resource not found")
        return self._content_by_uri[uri]


def _manager_with_clients(
    tmp_path,
    entries: dict[str, tuple[_FakeMcpClient, ServerStatus]],
) -> MCPServerManager:
    manager = MCPServerManager(config_path=tmp_path / ".mcp.json")
    for name, (client, status) in entries.items():
        state = MCPServerState(config=MCPServerConfig(name=name))
        state.client = client  # type: ignore[assignment]
        state.status = status
        manager._servers[name] = state
    return manager


def test_mcp_bridge_tools_deferred_in_default_toolset() -> None:
    """All bridges stay lazy and are activated by name through tool_search.

    The async-agent availability filter admits a whole ``mcp`` toolset, so
    placing these bridges there would widen background agents (see the
    cc-alignment registry test).
    """
    tools = (
        ListMcpResourcesTool(None),
        ReadMcpResourceTool(None),
        ListMcpResourceTemplatesTool(None),
        SubscribeMcpResourceTool(None),
        UnsubscribeMcpResourceTool(None),
        ListMcpResourceNotificationsTool(None),
        ListMcpPromptsTool(None),
        GetMcpPromptTool(None),
    )

    assert {tool.get_spec().toolset for tool in tools} == {"default"}
    assert {tool.get_spec().exposure for tool in tools} == {"deferred"}


def test_list_mcp_resources_lists_only_manager_connected_servers(tmp_path) -> None:
    docs = _FakeMcpClient(
        resources=[
            SimpleNamespace(
                uri="mcp://docs/schema",
                name="Schema",
                mime_type="application/json",
            )
        ]
    )
    stale = _FakeMcpClient(
        resources=[
            SimpleNamespace(
                uri="mcp://stale/secret",
                name="Stale",
                mime_type="text/plain",
            )
        ],
        connected=True,
    )
    disconnected = _FakeMcpClient(
        resources=[
            SimpleNamespace(
                uri="mcp://offline/ignored",
                name="Offline",
                mime_type="text/plain",
            )
        ],
        connected=False,
    )
    manager = _manager_with_clients(
        tmp_path,
        {
            "docs": (docs, ServerStatus.CONNECTED),
            "stale": (stale, ServerStatus.ERROR),
            "offline": (disconnected, ServerStatus.CONNECTED),
        },
    )

    result = asyncio.run(ListMcpResourcesTool(manager).execute({}))

    assert not result.is_error
    assert "URI: mcp://docs/schema" in result.content
    assert "Name: Schema" in result.content
    assert "MimeType: application/json" in result.content
    assert "Server: docs" in result.content
    assert "stale" not in result.content
    assert "offline" not in result.content
    assert docs.list_calls == 1
    assert stale.list_calls == 0
    assert disconnected.list_calls == 0


def test_read_mcp_resource_reads_first_connected_server_with_uri(tmp_path) -> None:
    missing = _FakeMcpClient(content_by_uri={})
    docs = _FakeMcpClient(content_by_uri={"mcp://docs/schema": '{"tables": ["users"]}'})
    manager = _manager_with_clients(
        tmp_path,
        {
            "missing": (missing, ServerStatus.CONNECTED),
            "docs": (docs, ServerStatus.CONNECTED),
        },
    )

    result = asyncio.run(
        ReadMcpResourceTool(manager).execute({"uri": "mcp://docs/schema"})
    )

    assert not result.is_error
    assert result.content == '{"tables": ["users"]}'
    assert missing.read_calls == ["mcp://docs/schema"]
    assert docs.read_calls == ["mcp://docs/schema"]


def test_read_mcp_resource_prefers_requested_server_when_uri_collides(tmp_path) -> None:
    first = _FakeMcpClient(content_by_uri={"mcp://shared/schema": "wrong schema"})
    docs = _FakeMcpClient(content_by_uri={"mcp://shared/schema": "docs schema"})
    manager = _manager_with_clients(
        tmp_path,
        {
            "first": (first, ServerStatus.CONNECTED),
            "docs": (docs, ServerStatus.CONNECTED),
        },
    )

    result = asyncio.run(
        ReadMcpResourceTool(manager).execute({
            "uri": "mcp://shared/schema",
            "server": "docs",
        })
    )

    assert not result.is_error
    assert result.content == "docs schema"
    assert first.read_calls == []
    assert docs.read_calls == ["mcp://shared/schema"]


def test_read_mcp_resource_reports_unknown_requested_server(tmp_path) -> None:
    docs = _FakeMcpClient(content_by_uri={"mcp://docs/schema": "docs schema"})
    manager = _manager_with_clients(
        tmp_path,
        {"docs": (docs, ServerStatus.CONNECTED)},
    )

    result = asyncio.run(
        ReadMcpResourceTool(manager).execute({
            "uri": "mcp://docs/schema",
            "server": "missing",
        })
    )

    assert result.is_error
    assert "MCP server is not connected: missing" in result.content
    assert docs.read_calls == []


def test_read_mcp_resource_stores_large_content_as_artifact(tmp_path) -> None:
    content = "\n".join(f"line {index}: {'x' * 40}" for index in range(80))
    docs = _FakeMcpClient(content_by_uri={"mcp://docs/large": content})
    manager = _manager_with_clients(
        tmp_path,
        {"docs": (docs, ServerStatus.CONNECTED)},
    )
    store = ArtifactStore(storage_dir=tmp_path / "artifacts")

    result = asyncio.run(
        ReadMcpResourceTool(manager, store).execute({"uri": "mcp://docs/large"})
    )

    assert not result.is_error
    assert result.artifact_id
    assert store.get(result.artifact_id) == content
    assert result.artifact_preview == "\n".join(content.split("\n")[:10])
    assert "Content saved as Artifact" in result.content
    store.shutdown()
