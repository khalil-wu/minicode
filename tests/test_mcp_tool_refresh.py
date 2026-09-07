from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp import types
from mcp.shared.exceptions import McpError

from backend.mcp.client import MCPClient, MCPServerCapabilities
from backend.mcp.manager import (
    MCPServerConfig,
    MCPServerManager,
    MCPServerState,
    ServerStatus,
)


def _runtime(root: Path) -> tuple[MCPServerManager, MCPServerState, MCPClient]:
    manager = MCPServerManager(root / ".mcp.json", workspace_root=None)
    config = MCPServerConfig(
        name="fixture", transport="http", url="https://unused.invalid/mcp"
    )
    client = manager._create_client(config)
    client._connected = True
    client._server_capabilities = MCPServerCapabilities(tools=True)
    state = MCPServerState(config=config, client=client, status=ServerStatus.CONNECTED)
    manager._servers[config.name] = state
    return manager, state, client


def _catalog(names: list[str]) -> dict:
    return {
        "tools": [
            {"name": name, "description": name, "inputSchema": {"type": "object"}}
            for name in names
        ]
    }


async def _notify(client: MCPClient) -> None:
    await client._sdk_message_handler(
        SimpleNamespace(root=types.ToolListChangedNotification())
    )


@pytest.mark.parametrize("first_fails", [False, True], ids=["success", "error"])
def test_refresh_retains_notifications_received_during_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, first_fails: bool
) -> None:
    async def scenario() -> None:
        manager, state, client = _runtime(tmp_path)
        started = asyncio.Event()
        release = asyncio.Event()
        names = ["old_tool"]
        requested: list[list[str]] = []

        async def request(method, params=None):
            assert method == "tools/list"
            snapshot = list(names)
            requested.append(snapshot)
            if len(requested) == 1:
                started.set()
                await release.wait()
                if first_fails:
                    raise McpError(types.ErrorData(code=-32603, message="temporary error"))
            return _catalog(snapshot)

        monkeypatch.setattr(client, "_request", request)
        await _notify(client)
        refresh = manager._tool_refresh_tasks["fixture"]
        await started.wait()
        names[:] = ["fresh_tool"]
        for notification_index in range(5):
            await _notify(client)
        release.set()
        await refresh

        assert requested == [["old_tool"], ["fresh_tool"]]
        assert [tool.name for tool in state.tools] == ["fresh_tool"]
        assert state.status == ServerStatus.CONNECTED
        assert state.last_error == ""
        assert state.last_exception is None
        assert state.operation_failures == {}
        assert manager._tool_refresh_tasks == {}
        assert manager._pending_tool_refreshes == {}

    asyncio.run(scenario())


def test_catalog_error_recovers_on_later_notification_without_losing_other_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        manager, state, client = _runtime(tmp_path)
        failure = McpError(types.ErrorData(code=-32603, message="temporary catalog error"))
        restore_failure = {"operation": "resource_restore", "message": "resource unavailable"}
        state.operation_failures["resource_restore"] = restore_failure
        requests: list[str] = []

        async def request(method, params=None):
            requests.append(method)
            if len(requests) == 1:
                raise failure
            return _catalog(["fresh_tool"])

        monkeypatch.setattr(client, "_request", request)
        await _notify(client)
        await manager._tool_refresh_tasks["fixture"]

        assert requests == ["tools/list"]
        assert state.status == ServerStatus.ERROR
        assert state.tools == []
        assert state.client is client
        assert client.connected
        assert state.last_exception is failure
        assert state.operation_failures["tool_catalog"]["message"] == str(failure)
        assert "temporary catalog error" in state.to_status_dict()["error"]
        assert manager.get_all_tools() == {}
        assert manager._tool_refresh_tasks == {}

        await _notify(client)
        await manager._tool_refresh_tasks["fixture"]

        assert requests == ["tools/list", "tools/list"]
        assert state.status == ServerStatus.CONNECTED
        assert state.last_error == ""
        assert state.last_exception is None
        assert state.operation_failures == {"resource_restore": restore_failure}
        assert [tool.name for tool in manager.get_all_tools()["fixture"]] == ["fresh_tool"]
        assert manager._registry_version == 2

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["connect", "reconnect", "cleanup", None])
def test_catalog_notifications_do_not_recover_lifecycle_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str | None
) -> None:
    async def scenario() -> None:
        manager, state, client = _runtime(tmp_path)
        state.status = ServerStatus.ERROR
        state.last_error = "lifecycle failure"
        if operation is not None:
            state.operation_failures["tool_catalog"] = {"message": "old catalog error"}
            state.operation_failures[operation] = {"message": "lifecycle failure"}

        async def request(method, params=None):
            pytest.fail("a catalog notification must not revive a failed lifecycle")

        monkeypatch.setattr(client, "_request", request)
        await _notify(client)
        await manager._tool_refresh_tasks["fixture"]
        await manager._handle_client_disconnect("fixture", client)

        assert state.status == ServerStatus.ERROR
        assert state.last_error == "lifecycle failure"
        assert state.client is client
        assert manager._reconnect_tasks == {}

    asyncio.run(scenario())


@pytest.mark.parametrize("stale_fails", [False, True], ids=["success", "error"])
def test_refresh_discards_stale_results_and_services_new_client_notifications(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stale_fails: bool
) -> None:
    async def scenario() -> None:
        manager, state, stale = _runtime(tmp_path)
        started = asyncio.Event()
        release = asyncio.Event()
        fresh = manager._create_client(state.config)
        fresh._connected = True
        fresh._server_capabilities = MCPServerCapabilities(tools=True)
        requests: list[str] = []

        async def stale_request(method, params=None):
            requests.append("stale")
            started.set()
            await release.wait()
            if stale_fails:
                raise ValueError("stale catalog failure")
            return _catalog(["stale_tool"])

        async def fresh_request(method, params=None):
            requests.append("fresh")
            return _catalog(["fresh_tool"])

        monkeypatch.setattr(stale, "_request", stale_request)
        monkeypatch.setattr(fresh, "_request", fresh_request)
        await _notify(stale)
        refresh = manager._tool_refresh_tasks["fixture"]
        await started.wait()
        state.client = fresh
        await _notify(fresh)
        await _notify(stale)
        release.set()
        await refresh

        assert requests == ["stale", "fresh"]
        assert state.client is fresh
        assert state.status == ServerStatus.CONNECTED
        assert [tool.name for tool in state.tools] == ["fresh_tool"]
        assert state.operation_failures == {}
        assert manager._pending_tool_refreshes == {}

    asyncio.run(scenario())


def test_stop_cancels_inflight_and_queued_catalog_refreshes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        manager, state, client = _runtime(tmp_path)
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def request(method, params=None):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        monkeypatch.setattr(client, "_request", request)
        await _notify(client)
        await started.wait()
        await _notify(client)
        assert await manager.stop_server("fixture")

        assert cancelled.is_set()
        assert state.status == ServerStatus.OFFLINE
        assert state.client is None
        assert not client.connected
        assert manager._tool_refresh_tasks == {}
        assert manager._pending_tool_refreshes == {}

    asyncio.run(scenario())


@pytest.mark.parametrize("transport", ["stdio", "http"])
def test_disconnect_after_catalog_error_keeps_transport_lifecycle_behavior(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, transport: str
) -> None:
    async def scenario() -> None:
        manager, state, client = _runtime(tmp_path)
        state.config.transport = transport
        reconnects: list[str] = []

        async def request(method, params=None):
            raise McpError(types.ErrorData(code=-32603, message="catalog error"))

        async def reconnect(name):
            reconnects.append(name)

        monkeypatch.setattr(client, "_request", request)
        monkeypatch.setattr(manager, "_try_automatic_remote_reconnect", reconnect)
        await _notify(client)
        await manager._tool_refresh_tasks["fixture"]
        client._connected = False
        await client._notify_disconnect()
        if transport == "http":
            await manager._reconnect_tasks["fixture"]
            assert reconnects == ["fixture"]
        else:
            assert reconnects == []
            assert state.last_error == "MCP stdio transport closed"
        assert state.client is None
        assert state.tools == []

    asyncio.run(scenario())
