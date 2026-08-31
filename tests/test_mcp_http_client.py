import asyncio

from mcp import types
from mcp.shared.exceptions import McpError

from backend.mcp.client import MCPClient, MCPTransport


def test_sse_uses_official_sse_transport_without_rewriting_url(monkeypatch) -> None:
    captured: dict[str, object] = {}
    sentinel = object()

    def fake_sse_client(url, **kwargs):
        captured.update(url=url, kwargs=kwargs)
        return sentinel

    monkeypatch.setattr("backend.mcp.client.sse_client", fake_sse_client)
    client = MCPClient(
        "figma-desktop",
        transport=MCPTransport.SSE,
        url="http://127.0.0.1:3845/mcp",
    )

    context = asyncio.run(client._sdk_transport_context())

    assert context is sentinel
    assert captured["url"] == "http://127.0.0.1:3845/mcp"
    assert captured["kwargs"]["timeout"] == 60.0
    assert captured["kwargs"]["sse_read_timeout"] == 100_000.0


def test_streamable_http_uses_official_transport_and_bearer_header(monkeypatch) -> None:
    class _Tokens:
        access_token = "secret-token"

        @staticmethod
        def is_expired() -> bool:
            return False

    captured: dict[str, object] = {}
    sentinel = object()

    def fake_streamable_client(url, **kwargs):
        captured.update(url=url, kwargs=kwargs)
        return sentinel

    monkeypatch.setattr("backend.mcp.client.streamablehttp_client", fake_streamable_client)
    client = MCPClient(
        "remote",
        transport=MCPTransport.HTTP,
        url="https://mcp.example/mcp",
        headers={"Authorization": "Bearer secret-token"},
    )

    context = asyncio.run(client._sdk_transport_context())

    assert context is sentinel
    assert captured["kwargs"]["headers"] == {"Authorization": "Bearer secret-token"}
    assert captured["kwargs"]["timeout"] == 60.0
    assert captured["kwargs"]["sse_read_timeout"] == 100_000.0


def test_call_tool_forwards_meta_through_official_session() -> None:
    class _Session:
        def __init__(self) -> None:
            self.call: dict[str, object] = {}

        async def call_tool(self, name, arguments, read_timeout_seconds, *, meta):
            self.call = {
                "name": name,
                "arguments": arguments,
                "timeout": read_timeout_seconds.total_seconds(),
                "meta": meta,
            }
            return types.CallToolResult(
                content=[types.TextContent(type="text", text="ok")],
                isError=False,
            )

    session = _Session()
    client = MCPClient("remote", transport=MCPTransport.HTTP, url="https://mcp.example/mcp")
    client._connected = True
    client._session = session

    result = asyncio.run(
        client.call_tool("search", {"query": "MCP"}, request_meta={"trace": "turn-1"})
    )

    assert result.text == "ok"
    assert session.call["meta"] == {"trace": "turn-1"}
    assert session.call["timeout"] == 100_000.0


def test_call_tool_preserves_official_sdk_rpc_error() -> None:
    class _Session:
        async def call_tool(self, *_args, **_kwargs):
            raise McpError(types.ErrorData(code=-32602, message="missing required field query"))

    client = MCPClient("remote", transport=MCPTransport.HTTP, url="https://mcp.example/mcp")
    client._connected = True
    client._session = _Session()

    result = asyncio.run(client.call_tool("search", {}))

    assert result.is_error is True
    assert "missing required field query" in result.text
    assert "Tool call timed out" not in result.text


def test_official_session_pagination_is_fully_consumed() -> None:
    class _Session:
        async def list_tools(self, cursor=None):
            if cursor is None:
                return types.ListToolsResult(
                    tools=[types.Tool(name="first", inputSchema={"type": "object"})],
                    nextCursor="page-2",
                )
            return types.ListToolsResult(
                tools=[types.Tool(name="second", inputSchema={"type": "object"})]
            )

    client = MCPClient("remote", transport=MCPTransport.HTTP, url="https://mcp.example/mcp")
    client._connected = True
    client._session = _Session()

    tools = asyncio.run(client.list_tools())

    assert [tool.name for tool in tools] == ["first", "second"]


def test_headers_helper_receives_minicode_server_env(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class _Process:
        returncode = 0

    async def fake_spawn_shell(command, **kwargs):
        captured.update(command=command, env=kwargs.get("env"))
        return _Process()

    async def fake_communicate_bounded(_process, **_kwargs):
        return b'{"X-Api-Key": "abc"}', b""

    monkeypatch.setattr("backend.mcp.client.spawn_shell", fake_spawn_shell)
    monkeypatch.setattr(
        "backend.mcp.client.communicate_bounded", fake_communicate_bounded
    )
    client = MCPClient(
        "remote",
        transport=MCPTransport.HTTP,
        url="https://mcp.example/mcp",
        headers_helper="echo-headers",
    )

    headers = asyncio.run(client._resolved_http_headers())

    env = captured["env"]
    assert isinstance(env, dict)
    assert env["MINICODE_MCP_SERVER_NAME"] == "remote"
    assert env["MINICODE_MCP_SERVER_URL"] == "https://mcp.example/mcp"
    assert "CLAUDE_CODE_MCP_SERVER_NAME" not in env
    assert "CLAUDE_CODE_MCP_SERVER_URL" not in env
    assert headers["X-Api-Key"] == "abc"
