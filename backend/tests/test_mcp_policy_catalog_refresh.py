from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from backend.mcp.client import MCPCallResult, MCPToolDef
from backend.mcp.manager import MCPServerConfig
from backend.mcp.registry import MCPToolProxy, MCPToolRegistry
from backend.tools.registry import ToolRegistry
from backend.tests.test_harness_mcp_services_regressions import MCPPeer, manager_fixture


@pytest.mark.asyncio
async def test_tool_policy_publication_preserves_transport_and_live_call_and_can_reenable(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    class Peer(MCPPeer):
        async def call_tool(self, name, arguments, **kwargs):
            self.calls.append((name, arguments))
            entered.set()
            await release.wait()
            return MCPCallResult([{"type": "text", "text": "Actual completed request"}])
    peer = Peer("policy", [MCPToolDef("read", "read", {"type": "object"}), MCPToolDef("write", "write", {"type": "object"})])
    manager = manager_fixture(tmp_path)
    manager._create_client = lambda config, **kwargs: peer
    config = MCPServerConfig("server", command="python")
    desired = config
    manager.load_config = lambda: [desired]
    await manager.start_server(config)
    registry = ToolRegistry()
    catalog = MCPToolRegistry(registry, mcp_manager=manager)
    catalog.register_server_tools("server", manager.get_all_tools()["server"], peer)
    prepared = registry.get_tool("mcp__server__write")
    running = asyncio.create_task(prepared.execute({"admitted": "before change"}))
    await entered.wait()
    first_version = manager.registry_version
    desired = replace(config, enabled_tools=["read"])
    await manager.reload_config()
    assert peer.closed == 0
    assert manager.get_client("server") is peer
    assert not running.done()
    assert manager.registry_version > first_version
    assert [tool.name for tool in manager.get_all_tools()["server"]] == ["read"]
    assert [tool.name for tool in manager._servers["server"].raw_tools] == ["read", "write"]
    assert (await prepared.execute({"admitted": "after disable"})).is_error
    release.set()
    observation = await running
    assert observation.content.startswith("Wall time:")
    assert observation.content.split("\nOutput:\n", 1)[1] == "Actual completed request"
    catalog.sync()
    assert registry.get_tool("mcp__server__write") is None
    desired = replace(config, disabled_tools=["read"])
    await manager.reload_config()
    catalog.sync()
    assert peer.closed == 0
    assert registry.get_tool("mcp__server__read") is None
    assert registry.get_tool("mcp__server__write") is not None
    before_approval = registry.get_tool("mcp__server__write")
    desired = replace(config, default_tools_approval_mode="prompt", supports_parallel_tool_calls=True)
    await manager.reload_config()
    assert peer.closed == 0
    assert (await before_approval.execute({})).is_error
    catalog.sync()
    updated = registry.get_tool("mcp__server__write")
    assert updated._approval_mode == "prompt"
    assert updated._supports_parallel_tool_calls is True
    desired = config
    await manager.reload_config()
    catalog.sync()
    assert set(manager.get_all_tools()["server"][i].name for i in range(2)) == {"read", "write"}
    assert peer.closed == 0
    await manager.stop_all()


@pytest.mark.asyncio
async def test_policy_update_waits_for_catalog_publication_without_republishing_old_raw_tools(tmp_path):
    peer = MCPPeer("refresh", [MCPToolDef("old", "old", {"type": "object"})])
    manager = manager_fixture(tmp_path)
    manager._create_client = lambda config, **kwargs: peer
    config = MCPServerConfig("server", command="python")
    await manager.start_server(config)
    peer.tools = [MCPToolDef("new", "new", {"type": "object"})]
    peer.list_entered.clear()
    peer.list_release = asyncio.Event()
    manager._schedule_tool_refresh("server", peer)
    await peer.list_entered.wait()
    manager.load_config = lambda: [replace(config, enabled_tools=["new"])]
    update = asyncio.create_task(manager.reload_config())
    await asyncio.sleep(0)
    assert not update.done()
    assert manager.get_all_tools() == {}
    peer.list_release.set()
    await update
    assert peer.closed == 0
    assert [tool.name for tool in manager.get_all_tools()["server"]] == ["new"]
    assert [tool.name for tool in manager._servers["server"].raw_tools] == ["new"]
    await manager.stop_all()


@pytest.mark.asyncio
async def test_transport_change_still_replaces_connection_and_server_disable_still_stops(tmp_path):
    manager = manager_fixture(tmp_path)
    peers = []
    def create(config, **kwargs):
        peer = MCPPeer(config.command)
        peers.append(peer)
        return peer
    manager._create_client = create
    config = MCPServerConfig("server", command="first")
    await manager.start_server(config)
    manager.load_config = lambda: [replace(config, command="second")]
    await manager.reload_config()
    assert peers[0].closed == 1
    assert manager.get_client("server") is peers[1]
    manager.load_config = lambda: [replace(config, command="second", auto_start=False)]
    await manager.reload_config()
    assert peers[1].closed == 1
    assert manager.get_client("server") is None
    assert manager.get_all_tools() == {}
    assert manager._servers["server"].raw_tools == []
    await manager.stop_all()
