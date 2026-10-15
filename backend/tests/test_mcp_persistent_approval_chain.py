from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.mcp.approval import MCPApprovalStore, apply_mcp_approval
from backend.mcp.client import MCPToolDef
from backend.mcp.config_file import write_mcp_config
from backend.mcp.manager import MCPServerManager, MCPServerState, ServerStatus
from backend.mcp.registry import MCPToolProxy
from backend.tests.test_codex_mcp_approval_producer import Transport


RAW_TOOL = "operation.raw"


def server_payload():
    return {"transport": "stdio", "command": "python", "args": ["peer.py"],
        "default_tools_approval_mode": "auto", "tools": {
            RAW_TOOL: {"output_token_limit": 73},
            "untouched": {"approval_mode": "prompt", "output_token_limit": 19},
        }}


def attach_proxy(manager, config):
    transport = Transport()
    definition = MCPToolDef(RAW_TOOL, "Original operation", {"type": "object", "properties": {}})
    manager._servers[config.name] = MCPServerState(config, client=transport, status=ServerStatus.CONNECTED, tools=[definition])
    manager.reload_config = AsyncMock()
    return MCPToolProxy(config.name, definition, transport, manager=manager)


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["user", "project"])
async def test_actual_custom_config_producer_persists_raw_tool_mode_to_its_own_file(tmp_path, monkeypatch, origin):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    user_path = tmp_path / "profile" / "data" / ".mcp.json"
    project_path = workspace / ".minicode" / "mcp.json"
    monkeypatch.setattr("backend.mcp.project_settings.is_workspace_trusted", lambda root: root.resolve() == workspace.resolve())
    selected = user_path if origin == "user" else project_path
    write_mcp_config(json.dumps({"servers": {"raw-server": server_payload()}}), selected)
    before = json.loads(selected.read_text(encoding="utf-8"))
    manager = MCPServerManager(config_path=user_path, workspace_root=workspace)
    [config] = manager._load_local_configs() if origin == "user" else manager._load_project_configs()
    assert config.source == origin
    tool = attach_proxy(manager, config)
    assert tool.approval_snapshot()["allow_persistent_approval"] is True
    assert tool.persistent_approval_target() == {"kind": "custom", "path": str(selected), "server": "raw-server"}
    owner = SimpleNamespace(mcp_tool_approvals=MCPApprovalStore())
    await apply_mcp_approval(tool, {"action": "approve", "remember_for_future": True}, owner)
    actual = json.loads(selected.read_text(encoding="utf-8"))
    expected = before["servers"]["raw-server"]
    expected["tools"][RAW_TOOL]["approval_mode"] = "approve"
    assert actual == {"servers": {"raw-server": expected}}
    assert (project_path if origin == "user" else user_path).exists() is False
    assert owner.mcp_tool_approvals.session_approved == {tool.session_approval_key()}
    manager.reload_config.assert_awaited_once_with()
    cold = MCPServerManager(config_path=user_path, workspace_root=workspace)
    [cold_config] = cold._load_local_configs() if origin == "user" else cold._load_project_configs()
    assert cold_config.tool_approval_modes[RAW_TOOL] == "approve"


@pytest.mark.asyncio
async def test_plugin_manifest_producer_persists_its_raw_namespace_in_user_settings(tmp_path, monkeypatch):
    from backend.plugins.manager import PluginManager

    plugin = tmp_path / "plugin"
    manifest_path = plugin / ".minicode-plugin" / "plugin.json"
    manifest_path.parent.mkdir(parents=True)
    manifest = {"name": "source-plugin", "version": "1.0.0", "mcp_servers": {"raw-server": server_payload()}}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    settings_path = tmp_path / "profile" / "settings.json"
    settings_path.parent.mkdir()
    before = {"untouched_setting": {"preserved": True}, "plugins": {"fixture@catalog": {"enabled": True,
        "mcp_servers": {"raw-server": {"tools": {"untouched": {"approval_mode": "prompt", "output_token_limit": 19}}}}}}}
    settings_path.write_text(json.dumps(before), encoding="utf-8")
    monkeypatch.setattr("backend.config_helpers.SETTINGS_FILE", settings_path)
    monkeypatch.setattr(PluginManager, "snapshot", lambda self: SimpleNamespace(enabled_plugins=[{
        "id": "fixture@catalog", "name": "source-plugin", "path": str(plugin), "manifest_paths": [str(manifest_path)],
    }]))
    stack = SimpleNamespace(effective_config=lambda: json.loads(settings_path.read_text(encoding="utf-8")))
    manager = MCPServerManager(config_path=tmp_path / "absent-mcp.json", workspace_root=tmp_path)
    [config] = manager._load_plugin_configs(config_stack=stack)
    assert config.name == "plugin:fixture@catalog:raw-server"
    tool = attach_proxy(manager, config)
    assert tool.approval_snapshot()["allow_persistent_approval"] is True
    assert tool.persistent_approval_target() == {"kind": "plugin", "plugin_id": "fixture@catalog", "server": "raw-server"}
    owner = SimpleNamespace(mcp_tool_approvals=MCPApprovalStore())
    await apply_mcp_approval(tool, {"action": "approve", "remember_for_future": True}, owner)
    actual = json.loads(settings_path.read_text(encoding="utf-8"))
    assert actual["untouched_setting"] == before["untouched_setting"]
    actual_server = actual["plugins"]["fixture@catalog"]["mcp_servers"]["raw-server"]
    assert actual_server["tools"][RAW_TOOL] == {"approval_mode": "approve"}
    assert actual_server["tools"]["untouched"] == before["plugins"]["fixture@catalog"]["mcp_servers"]["raw-server"]["tools"]["untouched"]
    assert json.loads(manifest_path.read_text(encoding="utf-8")) == manifest
    [cold] = manager._load_plugin_configs(config_stack=stack)
    assert cold.tool_approval_modes[RAW_TOOL] == "approve"
    assert cold.tool_output_token_limits[RAW_TOOL] == 73
    manager.reload_config.assert_awaited_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["prompt", "writes"])
async def test_real_config_prompt_and_writes_have_no_future_option_or_persistent_write(tmp_path, mode):
    config_path = tmp_path / ".mcp.json"
    payload = server_payload()
    payload["tools"][RAW_TOOL]["approval_mode"] = mode
    write_mcp_config(json.dumps({"servers": {"server": payload}}), config_path)
    manager = MCPServerManager(config_path=config_path, workspace_root=tmp_path)
    [config] = manager._load_local_configs()
    tool = attach_proxy(manager, config)
    assert tool.approval_snapshot()["allow_session_remember"] is False
    assert tool.approval_snapshot()["allow_persistent_approval"] is False
    owner = SimpleNamespace(mcp_tool_approvals=MCPApprovalStore())
    before = config_path.read_bytes()
    await apply_mcp_approval(tool, {"action": "approve", "remember_for_future": True, "remember_for_session": True}, owner)
    assert config_path.read_bytes() == before
    assert owner.mcp_tool_approvals.session_approved == set()
    manager.reload_config.assert_not_awaited()


@pytest.mark.asyncio
async def test_source_failed_persistent_edit_keeps_only_approved_thread_authority(tmp_path, monkeypatch, caplog):
    path = tmp_path / ".mcp.json"
    write_mcp_config(json.dumps({"servers": {"server": server_payload()}}), path)
    manager = MCPServerManager(config_path=path, workspace_root=tmp_path)
    [config] = manager._load_local_configs()
    tool = attach_proxy(manager, config)
    failure = AsyncMock(side_effect=OSError("Actual writable layer is unavailable"))
    monkeypatch.setattr("backend.mcp.approval.persist_mcp_approval", failure)
    owner = SimpleNamespace(mcp_tool_approvals=MCPApprovalStore())
    other = MCPApprovalStore()
    before = path.read_bytes()
    await apply_mcp_approval(tool, {"action": "approve", "remember_for_future": True}, owner)
    assert path.read_bytes() == before
    assert owner.mcp_tool_approvals.session_approved == {tool.session_approval_key()}
    assert other.session_approved == set()
    assert "Actual writable layer is unavailable" in caplog.text
    manager.reload_config.assert_not_awaited()
