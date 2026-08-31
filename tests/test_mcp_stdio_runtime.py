import asyncio
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from backend.mcp.client import MCPClient


def test_stdio_mcp_delegates_transport_and_session_lifecycle_to_official_sdk(monkeypatch) -> None:
    from mcp import types

    captured: dict[str, object] = {}

    @asynccontextmanager
    async def fake_stdio_client(parameters):
        captured["parameters"] = parameters
        yield object(), object()

    class _FakeSession:
        def __init__(self, read_stream, write_stream, **kwargs):
            captured["streams"] = (read_stream, write_stream)
            captured["session_kwargs"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            captured["session_closed"] = True

        async def initialize(self):
            return types.InitializeResult(
                protocolVersion=types.LATEST_PROTOCOL_VERSION,
                capabilities=types.ServerCapabilities(tools=types.ToolsCapability()),
                serverInfo=types.Implementation(name="fake", version="1.0"),
            )

    async def run() -> None:
        client = MCPClient(
            "official-sdk-runtime-test",
            command="python",
            args=["server.py"],
            startup_timeout=1.0,
            request_timeout=1.0,
            tool_timeout=1.0,
        )
        await client.connect()
        assert client.connected
        assert client.server_capabilities.tools
        await client.close()

    monkeypatch.setattr("backend.mcp.client.stdio_client", fake_stdio_client)
    monkeypatch.setattr("backend.mcp.client._LifecycleClientSession", _FakeSession)
    asyncio.run(run())

    parameters = captured["parameters"]
    assert parameters.command
    assert parameters.args == ["server.py"]
    assert captured["session_closed"] is True


def test_stdio_mcp_reports_immediate_process_exit() -> None:
    async def run() -> None:
        client = MCPClient(
            "immediate-exit-test",
            command="python",
            args=["-c", "raise SystemExit(7)"],
            startup_timeout=1.0,
            request_timeout=1.0,
            tool_timeout=1.0,
        )
        try:
            await client.connect()
        finally:
            await client.close()

    with pytest.raises(ConnectionError) as exc_info:
        asyncio.run(run())

    assert "immediate-exit-test" in str(exc_info.value)
    assert "connection failed" in str(exc_info.value)


def test_stdio_mcp_notifies_immediately_when_server_exits_after_catalog(
    tmp_path: Path,
) -> None:
    server = tmp_path / "exit_after_catalog.py"
    server.write_text(
        """
import json
import sys

for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    if method == "initialize":
        result = {
            "protocolVersion": message["params"]["protocolVersion"],
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "exit-after-catalog", "version": "1.0"},
        }
    elif method == "tools/list":
        result = {"tools": []}
    else:
        continue
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}) + "\\n")
    sys.stdout.flush()
    if method == "tools/list":
        break
""".strip(),
        encoding="utf-8",
    )

    async def run() -> None:
        disconnected = asyncio.Event()

        async def on_disconnect(_server_name: str) -> None:
            disconnected.set()

        client = MCPClient(
            "exit-after-catalog",
            command=sys.executable,
            args=[str(server)],
            on_disconnect=on_disconnect,
            startup_timeout=2.0,
            request_timeout=2.0,
            tool_timeout=2.0,
        )
        await client.connect()
        assert await client.list_tools() == []
        await asyncio.wait_for(disconnected.wait(), timeout=2.0)
        assert client.connected is False
        assert await client.close() is True

    asyncio.run(run())
