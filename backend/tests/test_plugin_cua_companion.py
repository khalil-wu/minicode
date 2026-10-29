"""Canonical plugin CUA declarations keep their policy and receiving owner."""
from dataclasses import replace
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.mcp.cua_repl import CuaReplCompanions, plugin_cua_declaration
from backend.mcp.connections import MCPConnection
from backend.mcp.environment_runtime import MCPEnvironmentRuntime
from backend.mcp.manager import MCPServerConfig, MCPServerManager, ServerStatus
from backend.plugins.manager import PluginManager, PluginSnapshot


def declared_plugin(tmp_path, name="computer-use", *, enabled=True, server_enabled=True):
    root = tmp_path / name
    manifest = root / ".codex-plugin/plugin.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"name": name, "mcp_servers": {
        "cua_repl": {"transport": "stdio", "command": "node", "args": ["declared.mjs"], "enabled": server_enabled,
            "env": {"CUA_REPL_ENABLED_SURFACES": "computer", "MINICODE_CUA_PROVIDER": "minicode-win32"}},
        "ordinary": {"transport": "stdio", "command": "ordinary-cli"}}}), encoding="utf-8")
    return {"id": name + "@fixture", "name": name, "path": str(root),
        "manifest_paths": [str(manifest)], "enabled": enabled}


def inventory(monkeypatch, plugins):
    monkeypatch.setattr(PluginManager, "snapshot", lambda self, **kwargs: PluginSnapshot({
        "plugins_enabled": True, "plugins": plugins}))


@pytest.mark.asyncio
async def test_real_plugin_manifest_produces_canonical_offline_cua_and_keeps_ordinary_names(tmp_path, monkeypatch):
    plugin = declared_plugin(tmp_path)
    inventory(monkeypatch, [plugin])
    manager = MCPServerManager(config_path=tmp_path / "mcp.json", workspace_root=tmp_path)
    configs = manager.load_config()
    cua = next(config for config in configs if config.name == "cua_repl")
    ordinary = next(config for config in configs if config.name.endswith(":ordinary"))
    assert (cua.plugin_id, cua.plugin_server_name, cua.source) == (plugin["id"], "cua_repl", "plugin:" + plugin["id"])
    assert ordinary.name == "plugin:" + plugin["id"] + ":ordinary"
    assert (ordinary.plugin_id, ordinary.plugin_server_name) == (plugin["id"], "ordinary")
    try:
        await manager.start_server(cua)
        assert manager._servers["cua_repl"].status == ServerStatus.OFFLINE
        assert manager.get_client("cua_repl") is None
    finally:
        await manager.stop_all()


def test_manual_canonical_override_and_first_plugin_precedence_are_preserved(tmp_path, monkeypatch):
    first, second = declared_plugin(tmp_path, "first"), declared_plugin(tmp_path, "second")
    inventory(monkeypatch, [first, second])
    path = tmp_path / "mcp.json"
    manager = MCPServerManager(config_path=path, workspace_root=tmp_path)
    assert next(config for config in manager.load_config() if config.name == "cua_repl").plugin_id == first["id"]
    path.write_text(json.dumps({"servers": {"cua_repl": {"transport": "stdio", "command": "manual-cli",
        "env": {"CUA_REPL_ENABLED_SURFACES": "computer"}}}}), encoding="utf-8")
    assert next(config for config in manager.load_config() if config.name == "cua_repl").command == "manual-cli"


def test_disabled_plugin_declaration_is_read_without_activation_or_bypassing_server_disable(tmp_path, monkeypatch):
    plugin = declared_plugin(tmp_path, enabled=False, server_enabled=False)
    inventory(monkeypatch, [plugin])
    manager = MCPServerManager(config_path=tmp_path / "mcp.json", workspace_root=tmp_path)
    assert manager.load_config() == []
    declaration = plugin_cua_declaration(manager, plugin)
    assert declaration is not None and not declaration.enabled
    assert declaration.plugin_id == plugin["id"] and manager.get_client("cua_repl") is None
    assert not manager._servers


def test_plugin_declaration_keeps_mcp_deny_policy_when_read_for_controls(tmp_path, monkeypatch):
    plugin = declared_plugin(tmp_path, enabled=False)
    inventory(monkeypatch, [plugin])
    managed = tmp_path / "managed"
    managed.mkdir()
    (managed / "managed-settings.json").write_text(json.dumps({"denied_mcp_servers": [{"name": "cua_repl"}]}))
    manager = MCPServerManager(config_path=tmp_path / "mcp.json", workspace_root=tmp_path, managed_settings_dir=managed)
    declaration = plugin_cua_declaration(manager, plugin)
    assert not declaration.enabled and declaration.disabled_reason == "Blocked by MiniCode MCP policy"
    assert not manager._servers


def test_environment_policy_matches_captured_declaration_not_generated_bootstrap(tmp_path):
    declaration = MCPServerConfig("cua_repl", command="node", args=["declared.mjs"], source="plugin:computer-use@fixture",
        plugin_id="computer-use@fixture", plugin_server_name="cua_repl")
    policy = {"plugins": {declaration.plugin_id: {"mcp_servers": {"cua_repl": {"identity": {
        "command": {"executable": "node", "args": [{"match": "exact", "value": "declared.mjs"}]}}}}}}}
    selection = SimpleNamespace(environment_id="local", config=SimpleNamespace(kind="Ready", value=SimpleNamespace(mcp_policy=policy)))
    runtime = MCPEnvironmentRuntime(None, None, local_cwd=str(tmp_path))
    runtime.snapshot = SimpleNamespace(all_selections=lambda: (selection,))
    runtime.cua_repl_config = declaration
    generated = replace(declaration, source="builtin:cua_repl:captured", args=["--eval", "generated native bootstrap"])
    assert runtime.apply_authority(generated).enabled
    selection.config.value.mcp_policy = {"plugins": {declaration.plugin_id: {"mcp_servers": {}}}}
    assert not runtime.apply_authority(generated).enabled


def test_installed_plugin_server_obeys_plugin_environment_policy(tmp_path, monkeypatch):
    plugin = declared_plugin(tmp_path)
    inventory(monkeypatch, [plugin])
    manager = MCPServerManager(config_path=tmp_path / "mcp.json", workspace_root=tmp_path)
    ordinary = next(config for config in manager.load_config() if config.name.endswith(":ordinary"))
    selection = SimpleNamespace(environment_id="local", config=SimpleNamespace(kind="Ready", value=SimpleNamespace(
        mcp_policy={"plugins": {plugin["id"]: {"mcp_servers": {"ordinary": {"identity": {
            "command": {"executable": "ordinary-cli", "args": []}}}}}}})))
    runtime = MCPEnvironmentRuntime(None, None, local_cwd=str(tmp_path))
    runtime.snapshot = SimpleNamespace(all_selections=lambda: (selection,))
    assert runtime.apply_authority(ordinary).enabled
    selection.config.value.mcp_policy = {"plugins": {plugin["id"]: {"mcp_servers": {}}}}
    assert not runtime.apply_authority(ordinary).enabled


@pytest.mark.asyncio
async def test_same_receiving_scope_cannot_reuse_another_plugins_companion(tmp_path):
    selected = MCPServerConfig("cua_repl", command="node", source="plugin:first@fixture", plugin_id="first@fixture",
        plugin_server_name="cua_repl", env={"CUA_REPL_ENABLED_SURFACES": "computer", "MINICODE_CUA_PROVIDER": "minicode-win32"})
    attachment = SimpleNamespace(is_remote=False, peer_info={"platformOs": "windows"}, environment=object(),
        permission_scope=SimpleNamespace(identity=lambda: {"scope": "same-original-scope"}),
        config=SimpleNamespace(mcp_policy=None, shell_environment_policy=SimpleNamespace(inherit="core",
            ignore_default_excludes=False, exclude=(), set_values={}, include_only=())),
        selection=SimpleNamespace(cwd=tmp_path.as_uri(), environment_id="local"))
    owner = CuaReplCompanions()
    first = await owner.admit(attachment, None, selected)
    original = owner.instances[owner.active_key]
    original.client = SimpleNamespace(close_thread=AsyncMock())
    async def close(client):
        await client.close_thread()
    original.connection = MCPConnection(factory=AsyncMock(), close=close, identity=original.key)
    original.connection.client = original.client
    second = await owner.admit(attachment, None, replace(selected, source="plugin:second@fixture", plugin_id="second@fixture"))
    assert first.source != second.source and second.plugin_id == "second@fixture"
    original.client.close_thread.assert_awaited_once()
    assert original.closed
    await owner.aclose()
