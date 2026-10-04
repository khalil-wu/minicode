from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from backend.bootstrap.app import AppBootstrap
from backend.mcp.manager import MCPServerConfig, MCPServerManager, MCPServerState


@pytest.fixture
def shutdown_services(monkeypatch):
    import backend.lsp.client as lsp
    import backend.memory.generation as memory
    import backend.preview.launcher as preview

    calls = []

    async def drain_memory(*, timeout):
        calls.append("memory")
        return []

    async def close_lsp():
        calls.append("lsp")

    async def close_previews():
        calls.append("preview")

    monkeypatch.setattr(memory, "drain_memory_background_tasks", drain_memory)
    monkeypatch.setattr(lsp, "get_lsp_manager", lambda: SimpleNamespace(shutdown_all=close_lsp))
    monkeypatch.setattr(preview, "stop_all_preview_launches", close_previews)
    return calls


def bootstrap():
    async def on_status(*args):
        return None

    return AppBootstrap(
        build_tool_registry=lambda *args, **kwargs: None,
        create_session_llm=lambda *args, **kwargs: None,
        ws_manager=SimpleNamespace(_sessions={}),
        on_mcp_status_change=on_status,
    )


def controlled_manager(monkeypatch, name, results):
    manager = MCPServerManager(workspace_root=None)
    manager._servers[name] = MCPServerState(MCPServerConfig(name=name))

    async def stop_server(server_name):
        assert server_name == name
        return results.pop(0)

    monkeypatch.setattr(manager, "stop_server", stop_server)
    return manager


@pytest.mark.asyncio
async def test_failed_pr_monitor_cannot_skip_other_service_shutdown(shutdown_services):
    owner = bootstrap()

    async def failed_poll():
        raise RuntimeError("controlled PR poll failure")

    owner._pr_monitor_task = asyncio.create_task(failed_poll())
    await asyncio.sleep(0)
    await owner.shutdown()
    assert shutdown_services == ["memory", "lsp", "preview"]
    assert owner._pr_monitor_task is None


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked_is_active", [False, True])
async def test_incomplete_real_mcp_stop_retains_only_its_owner_until_retry(
    monkeypatch, shutdown_services, blocked_is_active,
):
    owner = bootstrap()
    pending = controlled_manager(monkeypatch, "pending", [False, True])
    stopped = controlled_manager(monkeypatch, "stopped", [True])
    owner._mcp_managers = {"pending-a": pending, "pending-b": pending, "stopped": stopped}
    owner.mcp_manager = pending if blocked_is_active else stopped

    await owner.shutdown()
    assert owner._mcp_managers == {"pending-a": pending, "pending-b": pending}
    assert owner.mcp_manager is (pending if blocked_is_active else None)
    assert shutdown_services == ["memory", "lsp", "preview"]

    await owner.shutdown()
    assert owner._mcp_managers == {}
    assert owner.mcp_manager is None
    assert shutdown_services == ["memory", "lsp", "preview"] * 2


@pytest.mark.asyncio
async def test_successful_mcp_shutdown_releases_aliases_and_active_owner(monkeypatch, shutdown_services):
    owner = bootstrap()
    stopped = controlled_manager(monkeypatch, "stopped", [True])
    owner._mcp_managers = {"a": stopped, "b": stopped}
    owner.mcp_manager = stopped
    await owner.shutdown()
    assert owner._mcp_managers == {}
    assert owner.mcp_manager is None
    assert shutdown_services == ["memory", "lsp", "preview"]
