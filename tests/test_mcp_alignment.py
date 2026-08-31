from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from mcp import types

from backend.mcp.client import MCPClient, MCPToolDef, MCPTransport
from backend.mcp.manager import (
    MCP_DEFAULT_STARTUP_TIMEOUT_SECONDS,
    MCP_DEFAULT_TOOL_TIMEOUT_SECONDS,
    MCP_REQUEST_TIMEOUT_SECONDS,
    MCPServerConfig,
    MCPServerManager,
    MCPServerState,
    ServerStatus,
    _filter_mcp_tools,
    _mcp_connection_timeout_seconds,
    _mcp_tool_timeout_seconds,
    validate_mcp_server_config,
)
from backend.mcp.registry import MCPToolProxy
from backend.tools.base import PermissionLevel


def _manual_manager(monkeypatch, tmp_path) -> MCPServerManager:
    manager = MCPServerManager(
        config_path=tmp_path / ".mcp.json",
    )
    monkeypatch.setattr(manager, "_load_plugin_configs", lambda **_kwargs: [])
    monkeypatch.setattr(manager, "_load_project_configs", lambda: [])
    return manager


def test_transport_preserves_the_explicit_minicode_config(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / ".mcp.json"
    config_path.write_text(
        json.dumps({
            "servers": {
                "user-http": {
                    "transport": "http",
                    "url": "https://user.example/mcp",
                }
            }
        }),
        encoding="utf-8",
    )
    manager = MCPServerManager(config_path=config_path)
    monkeypatch.setattr(manager, "_load_plugin_configs", lambda **_kwargs: [])
    monkeypatch.setattr(manager, "_load_project_configs", lambda: [])

    configs = {config.name: config for config in manager.load_config()}

    assert configs["user-http"].transport == "http"
    assert configs["user-http"].url == "https://user.example/mcp"


def test_local_config_errors_are_visible_without_last_good_fallback(monkeypatch, tmp_path) -> None:
    manager = _manual_manager(monkeypatch, tmp_path)
    manager._config_path.write_text(
        json.dumps({
            "servers": {
                "docs": {"transport": "stdio", "command": "node", "auto_start": False}
            }
        }),
        encoding="utf-8",
    )

    assert [config.name for config in manager.load_config()] == ["docs"]

    manager._config_path.write_text("{broken", encoding="utf-8")
    with pytest.raises(RuntimeError, match="Failed to read"):
        manager.load_config()

    manager._config_path.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="JSON object"):
        manager.load_config()

    manager._config_path.unlink()
    assert manager.load_config() == []


def test_reload_rebuilds_runtime_order_from_effective_catalog(monkeypatch, tmp_path) -> None:
    manager = _manual_manager(monkeypatch, tmp_path)

    async def scenario() -> None:
        manager._config_path.write_text(
            json.dumps({
                "servers": {
                    "alpha": {"transport": "stdio", "command": "alpha", "auto_start": False},
                    "beta": {"transport": "stdio", "command": "beta", "auto_start": False},
                }
            }),
            encoding="utf-8",
        )
        await manager.reload_config()
        assert list(manager._servers) == ["alpha", "beta"]

        manager._config_path.write_text(
            json.dumps({
                "servers": {
                    "beta": {"transport": "stdio", "command": "beta", "auto_start": False},
                    "alpha": {"transport": "stdio", "command": "alpha", "auto_start": False},
                }
            }),
            encoding="utf-8",
        )
        await manager.reload_config()
        assert list(manager._servers) == ["beta", "alpha"]

    asyncio.run(scenario())


def test_timeout_defaults_preserve_explicit_config() -> None:
    config = MCPServerConfig(
        name="remote",
        transport="http",
        url="https://mcp.example/mcp",
        startup_timeout_sec=12,
        tool_timeout_sec=45,
    )

    assert MCP_DEFAULT_STARTUP_TIMEOUT_SECONDS == 30.0
    assert MCP_REQUEST_TIMEOUT_SECONDS == 60.0
    assert MCP_DEFAULT_TOOL_TIMEOUT_SECONDS == 100_000.0
    assert _mcp_connection_timeout_seconds() == 30.0
    assert _mcp_tool_timeout_seconds() == 100_000.0
    assert _mcp_connection_timeout_seconds(config) == 12.0
    assert _mcp_tool_timeout_seconds(config) == 45.0
    assert _mcp_tool_timeout_seconds(MCPServerConfig(name="websearch")) == 100_000.0

def test_client_uses_only_granular_timeout_budgets() -> None:
    granular = MCPClient("granular", startup_timeout=11, request_timeout=12, tool_timeout=13)
    assert granular._startup_timeout == 11.0
    assert granular._request_timeout == 12.0
    assert granular._tool_timeout == 13.0


def test_removed_timeout_and_retry_fields_are_not_part_of_the_runtime_api() -> None:
    with pytest.raises(TypeError):
        MCPClient("invalid", timeout=8)
    with pytest.raises(TypeError):
        MCPServerConfig(name="invalid", max_retries=5)


def test_enabled_filter_runs_before_disabled_filter() -> None:
    config = MCPServerConfig(
        name="docs",
        command="node",
        enabled_tools=["read", "write"],
        disabled_tools=["write"],
    )
    tools = [
        MCPToolDef(name="read", description=""),
        MCPToolDef(name="write", description=""),
        MCPToolDef(name="search", description=""),
    ]

    assert [tool.name for tool in _filter_mcp_tools(config, tools)] == ["read"]


def test_required_startup_failures_are_aggregated(monkeypatch) -> None:
    manager = MCPServerManager()
    configs = [
        MCPServerConfig(name="first", command="first", required=True),
        MCPServerConfig(name="second", command="second", required=True),
    ]
    monkeypatch.setattr(manager, "load_config", lambda: configs)

    async def fail(config: MCPServerConfig, **_kwargs) -> None:
        state = MCPServerState(config=config)
        state.status = ServerStatus.ERROR
        manager._servers[config.name] = state

    monkeypatch.setattr(manager, "start_server", fail)

    with pytest.raises(RuntimeError, match="first, second"):
        asyncio.run(manager.start_all())


class _PolicyManager:
    def __init__(self, config: MCPServerConfig) -> None:
        self.config = config

    def get_server_config(self, _name: str) -> MCPServerConfig:
        return self.config

    def get_client(self, _name: str):
        return None


@pytest.mark.parametrize(
    "mode, read_only, expected",
    [
        ("auto", True, PermissionLevel.AUTO),
        ("auto", False, PermissionLevel.CONFIRM),
        ("prompt", True, PermissionLevel.CONFIRM),
        ("prompt", False, PermissionLevel.CONFIRM),
        ("writes", True, PermissionLevel.AUTO),
        ("writes", False, PermissionLevel.CONFIRM),
        ("approve", True, PermissionLevel.AUTO),
        ("approve", False, PermissionLevel.AUTO),
    ],
)
def test_minicode_mcp_approval_modes(mode, read_only, expected) -> None:
    config = MCPServerConfig(
        name="remote",
        transport="http",
        url="https://mcp.example/mcp",
        default_tools_approval_mode=mode,
    )
    manager = _PolicyManager(config)
    proxy = MCPToolProxy(
        "remote",
        MCPToolDef(name="operate", description="", annotations={"readOnlyHint": read_only}),
        manager,
        manager=manager,
    )

    assert proxy.permission == expected


def test_per_tool_approval_override_and_parallel_policy() -> None:
    config = MCPServerConfig(
        name="remote",
        transport="http",
        url="https://mcp.example/mcp",
        supports_parallel_tool_calls=True,
        default_tools_approval_mode="prompt",
        tool_approval_modes={"read": "approve"},
    )
    manager = _PolicyManager(config)
    proxy = MCPToolProxy(
        "remote",
        MCPToolDef(name="read", description="", annotations={"readOnlyHint": False}),
        manager,
        manager=manager,
    )

    assert proxy.permission == PermissionLevel.AUTO
    assert proxy.is_concurrency_safe() is True


def test_tool_list_changed_notification_invokes_refresh_callback() -> None:
    changed: list[str] = []

    async def on_changed(name: str) -> None:
        changed.append(name)

    client = MCPClient(
        "remote",
        transport=MCPTransport.HTTP,
        url="https://mcp.example/mcp",
        on_tools_changed=on_changed,
    )
    message = SimpleNamespace(root=types.ToolListChangedNotification())

    asyncio.run(client._sdk_message_handler(message))

    assert changed == ["remote"]


class _RefreshClient:
    def __init__(self, tools: list[MCPToolDef], *, block: bool = False) -> None:
        self.connected = True
        self.tools = tools
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        if not block:
            self.release.set()
        self.closed = False

    async def list_tools(self) -> list[MCPToolDef]:
        self.started.set()
        await self.release.wait()
        return self.tools

    async def close(self) -> None:
        self.closed = True
        self.connected = False


def test_tool_refresh_reapplies_filters_and_fences_stale_client() -> None:
    async def scenario() -> None:
        manager = MCPServerManager()
        config = MCPServerConfig(
            name="remote",
            transport="http",
            url="https://mcp.example/mcp",
            enabled_tools=["read", "write"],
            disabled_tools=["write"],
        )
        fresh = _RefreshClient([
            MCPToolDef(name="read", description=""),
            MCPToolDef(name="write", description=""),
        ])
        state = MCPServerState(config=config, client=fresh, status=ServerStatus.CONNECTED)
        manager._servers["remote"] = state

        manager._schedule_tool_refresh("remote", fresh)
        await manager._tool_refresh_tasks["remote"]
        assert [tool.name for tool in state.tools] == ["read"]

        stale = _RefreshClient([MCPToolDef(name="stale", description="")], block=True)
        state.client = stale
        manager._schedule_tool_refresh("remote", stale)
        task = manager._tool_refresh_tasks["remote"]
        await stale.started.wait()
        state.client = fresh
        stale.release.set()
        await task
        assert [tool.name for tool in state.tools] == ["read"]

    asyncio.run(scenario())


def test_stopping_server_cancels_pending_tool_refresh() -> None:
    async def scenario() -> None:
        manager = MCPServerManager()
        config = MCPServerConfig(
            name="remote",
            transport="http",
            url="https://mcp.example/mcp",
        )
        client = _RefreshClient([MCPToolDef(name="read", description="")], block=True)
        manager._servers["remote"] = MCPServerState(
            config=config,
            client=client,
            status=ServerStatus.CONNECTED,
        )
        manager._schedule_tool_refresh("remote", client)
        await client.started.wait()

        await manager.stop_server("remote")

        assert "remote" not in manager._tool_refresh_tasks
        assert client.closed is True
        assert manager._servers["remote"].tools == []

    asyncio.run(scenario())
