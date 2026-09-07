import asyncio
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from backend.mcp.client import MCPClient
from backend.mcp.manager import MCPServerConfig, MCPServerManager, ServerStatus
from backend.services.mcp_service import list_mcp_inventory


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


def test_stdio_catalog_notifications_survive_startup_refresh_and_catalog_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def run() -> None:
        notices = [asyncio.Event(), asyncio.Event()]
        notifications: list[str] = []
        statuses: list[ServerStatus] = []
        fresh = asyncio.Event()

        async def on_status(name, status):
            statuses.append(status)
            if [tool.name for tool in manager._servers[name].tools] == ["fresh_tool"]:
                fresh.set()

        manager = MCPServerManager(
            tmp_path / ".mcp.json", workspace_root=None, on_status_change=on_status
        )
        create_client = manager._create_client

        def instrument_client(config, **kwargs):
            client = create_client(config, **kwargs)
            on_changed = client._on_tools_changed

            async def record_notification(name):
                await on_changed(name)
                notifications.append(name)
                if len(notifications) <= len(notices):
                    notices[len(notifications) - 1].set()

            client._on_tools_changed = record_notification
            return client

        monkeypatch.setattr(manager, "_create_client", instrument_client)
        config = MCPServerConfig(
            name="catalog-fixture",
            command=sys.executable,
            args=[str(Path(__file__).parent / "fixtures" / "mcp_catalog_server.py")],
            startup_timeout_sec=3,
        )
        startup = asyncio.create_task(manager.start_server(config))
        client = None
        try:
            async with asyncio.timeout(10):
                await notices[0].wait()
                state = manager._servers[config.name]
                client = state.client
                assert state.status == ServerStatus.STARTING
                state.operation_failures["reconnect"] = {"message": "previous attempt failed"}
                await client._session.send_ping()
                await startup
                await notices[1].wait()
                assert state.status == ServerStatus.CONNECTED
                refresh = manager._tool_refresh_tasks[config.name]
                await client._session.send_ping()
                await fresh.wait()
                await refresh

            assert notifications == [config.name] * 3
            assert ServerStatus.ERROR in statuses
            assert state.status == ServerStatus.CONNECTED
            assert state.last_error == ""
            assert state.operation_failures == {}
            assert state.client is client
            assert client.connected
            assert [(tool.name, tool.description) for tool in state.tools] == [
                ("fresh_tool", "catalog request 4")
            ]
            assert manager._tool_refresh_tasks == {}
            assert manager._pending_tool_refreshes == {}
        finally:
            if not startup.done():
                startup.cancel()
            await asyncio.gather(startup, return_exceptions=True)
            assert await manager.stop_server(config.name)
        assert client is not None and client._lifecycle_task is None
        assert manager._servers[config.name].status == ServerStatus.OFFLINE

    asyncio.run(run())


def _contract_config(mode: str) -> MCPServerConfig:
    return MCPServerConfig(
        name="contract-fixture",
        command=sys.executable,
        args=[str(Path(__file__).parent / "fixtures" / "mcp_contract_server.py"), mode],
        startup_timeout_sec=3,
    )


def test_stdio_resources_and_prompts_work_without_tools_capability(tmp_path: Path) -> None:
    async def run() -> None:
        manager = MCPServerManager(tmp_path / ".mcp.json", workspace_root=None)
        config = _contract_config("inventory-only")
        try:
            await manager.start_server(config)
            state = manager._servers[config.name]
            assert state.status == ServerStatus.CONNECTED
            assert state.tools == []
            client = manager.get_client(config.name)
            assert client is not None
            assert not client.server_capabilities.tools
            assert client.server_capabilities.resources
            assert client.server_capabilities.prompts

            inventory = await list_mcp_inventory(manager, config.name)
            assert [resource["uri"] for resource in inventory["resources"]] == ["fixture://guide"]
            assert [prompt["name"] for prompt in inventory["prompts"]] == ["review"]
            assert await client.read_resource("fixture://guide") == "RESOURCE_MARKER"
            assert await client.get_prompt("review") == "user: PROMPT_MARKER"
        finally:
            assert await manager.stop_server(config.name)

    asyncio.run(run())


def test_stdio_invalid_tool_result_preserves_live_client_and_stop_ownership(tmp_path: Path) -> None:
    async def run() -> None:
        manager = MCPServerManager(tmp_path / ".mcp.json", workspace_root=None)
        config = _contract_config("tools")
        client = None
        try:
            await manager.start_server(config)
            client = manager.get_client(config.name)
            assert client is not None
            lifecycle = client._lifecycle_task

            invalid = await client.call_tool("inspect", {"behavior": "invalid"})
            assert invalid.is_error
            assert "Invalid structured content" in invalid.text
            await client._session.send_ping()
            assert manager._servers[config.name].status == ServerStatus.CONNECTED
            assert manager.get_client(config.name) is client
            assert client.connected
            assert client._lifecycle_task is lifecycle and not lifecycle.done()

            valid = await client.call_tool("inspect", {"behavior": "valid"})
            assert not valid.is_error
            assert valid.text == "TOOL_MARKER"
        finally:
            try:
                assert await manager.stop_server(config.name)
                if client is not None:
                    assert client._lifecycle_task is None
            finally:
                if client is not None:
                    await client.close()
        assert lifecycle.done()
        assert client._lifecycle_task is None
        assert manager._servers[config.name].status == ServerStatus.OFFLINE

    asyncio.run(run())


def test_stdio_actual_transport_exit_still_notifies_manager_after_tool_call(tmp_path: Path) -> None:
    async def run() -> None:
        disconnected = asyncio.Event()

        async def on_status(name, status):
            if status == ServerStatus.ERROR:
                disconnected.set()

        manager = MCPServerManager(
            tmp_path / ".mcp.json", workspace_root=None, on_status_change=on_status
        )
        config = _contract_config("tools")
        client = None
        try:
            await manager.start_server(config)
            client = manager.get_client(config.name)
            assert client is not None
            lifecycle = client._lifecycle_task
            async with asyncio.timeout(5):
                result = await client.call_tool("inspect", {"behavior": "exit"})
                assert result.is_error
                await disconnected.wait()
                await lifecycle
            state = manager._servers[config.name]
            assert state.status == ServerStatus.ERROR
            assert state.last_error == "MCP stdio transport closed"
            assert state.client is None
            assert not client.connected
            assert manager._reconnect_tasks == {}
        finally:
            assert await manager.stop_server(config.name)
            if client is not None:
                await client.close()

    asyncio.run(run())
