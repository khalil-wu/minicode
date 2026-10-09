"""MCP UI, schemas and resource bridges keep the same projectless owner."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.api import _state
from backend.artifact.store import ArtifactStore
from backend.bootstrap.app import AppBootstrap
from backend.config import AppConfig, LLMSettings
from backend.mcp.client import MCPToolDef
from backend.mcp.manager import MCPServerManager
from backend.services.tool_registry_factory import build_tool_registry
from backend.ws.handler import WebSocketSession
from backend.ws.handlers.mcp import handle_mcp_list
from backend.ws.session_lifecycle import SessionLifecycle


def _bootstrap(builder):
    return AppBootstrap(
        build_tool_registry=builder,
        create_session_llm=lambda **kwargs: None,
        ws_manager=SimpleNamespace(),
        on_mcp_status_change=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_empty_projectless_mcp_list_is_an_empty_inventory_after_workspace_clear(tmp_path, monkeypatch):
    global_manager = MCPServerManager(config_path=tmp_path / "global-mcp.json", workspace_root=None)
    bootstrap = _bootstrap(build_tool_registry)
    bootstrap._mcp_managers[bootstrap._mcp_workspace_key(None)] = global_manager
    monkeypatch.setattr(_state, "bootstrap", bootstrap)
    monkeypatch.setattr("backend.services.mcp_service._read_current_config_data", lambda: {"servers": {}})
    session = SimpleNamespace(
        active_conversation_id="unbound", mcp_manager=object(),
        refresh_tool_registry_if_mcp_changed=lambda **kwargs: None,
        send_payload=AsyncMock(), send_event=AsyncMock(),
    )
    lifecycle = SessionLifecycle(session)
    lifecycle.clear_workspace_runtime()
    assert session.mcp_manager is global_manager
    await handle_mcp_list(session, {})
    assert session.send_payload.await_args.args[0]["servers"] == []
    assert session.send_payload.await_args.args[0]["workspace_root"] == ""
    result = session.send_event.await_args.args[0]
    assert result.data["command"] == "mcp.list" and result.data.get("level") != "error"


def _manager(scope, label):
    client = SimpleNamespace(connected=True, instructions="")
    return SimpleNamespace(
        workspace_root=scope, registry_version=1,
        get_all_tools=lambda: {label: [MCPToolDef("read", label, annotations={"readOnlyHint": True})]},
        get_client=lambda name: client,
        get_server_contract=lambda name: (label,),
        get_server_config=lambda name: None,
    )


def test_clearing_workspace_refreshes_global_schema_even_before_old_active_record_is_removed(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    global_manager, project_manager = _manager(None, "global"), _manager(project, "project")
    bootstrap = _bootstrap(build_tool_registry)
    bootstrap._mcp_managers[bootstrap._mcp_workspace_key(None)] = global_manager
    bootstrap._mcp_managers[bootstrap._mcp_workspace_key(project)] = project_manager
    bootstrap.mcp_manager = project_manager
    monkeypatch.setattr(_state, "bootstrap", bootstrap)
    config = AppConfig(llm=LLMSettings(api_key=""))
    monkeypatch.setattr("backend.ws.handler.load_config", lambda **kwargs: config)
    session = object.__new__(WebSocketSession)
    session.conversation_runtime = SimpleNamespace(active_conversation_id="old-project")
    session.conversation_repo = SimpleNamespace(get_conversation_summary=lambda name: SimpleNamespace(workspace_root=str(project), worktree_path=""))
    session.session_lifecycle = SessionLifecycle(session)
    session.session_lifecycle.workspace_root = project
    session.mcp_manager = project_manager
    session._mcp_registry_version_snapshot = 1
    session._mcp_manager_snapshot_id = id(project_manager)
    session.artifact_store = ArtifactStore(storage_dir=tmp_path / "artifacts")
    session._has_active_run = lambda: False
    session.tool_registry = build_tool_registry(session.artifact_store, workspace_root=project, config=config, mcp_manager=project_manager)
    assert "mcp__project__read" in session.tool_registry.list_tools()
    session.session_lifecycle.clear_workspace_runtime()
    assert session.active_conversation_id == "old-project"
    assert session.mcp_manager is global_manager
    assert "mcp__global__read" in session.tool_registry.list_tools()
    assert "mcp__project__read" not in session.tool_registry.list_tools()
    bridge = session.tool_registry.get_tool("list_mcp_resources")
    assert bridge._mcp_manager is global_manager


def test_explicit_pending_mcp_owner_cannot_inherit_bootstrap_active_project(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    manager = _manager(project, "project")
    bootstrap = _bootstrap(build_tool_registry)
    bootstrap.mcp_manager = manager
    bootstrap._mcp_managers[bootstrap._mcp_workspace_key(project)] = manager
    monkeypatch.setattr(_state, "bootstrap", bootstrap)
    store = ArtifactStore(storage_dir=tmp_path / "artifacts")
    config = AppConfig(llm=LLMSettings(api_key=""))
    registry = bootstrap.create_tool_registry(store, workspace_root=None, config=config, mcp_manager=None)
    assert not any(name.startswith("mcp__") for name in registry.list_tools())
    assert registry.get_tool("list_mcp_resources")._mcp_manager is None
    scoped = bootstrap.create_tool_registry(store, workspace_root=project, config=config)
    assert "mcp__project__read" in scoped.list_tools()
