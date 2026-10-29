"""Format-specific plugin declarations reach inventory and real execution together."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil

import pytest

from backend import config, config_helpers
from backend.commands import plugins as commands
from backend.config_layers import ConfigLayer, ConfigLayerSource, ConfigLayerStack
from backend.hooks.discovery import discover_hook_snapshot
from backend.hooks.manager import HookManager
from backend.mcp.agent_plugin_config import AGENT_PLUGIN_MCP_SCHEMA, parse_agent_plugin_mcp_config
from backend.mcp.manager import MCPServerManager, ServerStatus
from backend.plugins.layout import agent_plugin_data_root
from backend.plugins.manifest import _load_plugin_manifest, plugin_skill_sources
from backend.plugins.policy import ManagedPluginPolicy
from backend.plugins.package import validate_plugin_directory
from backend.plugins.source_agent_manifest import AGENT_PLUGIN_SCHEMA
from backend.services import plugin_settings_service as service
from backend.skills.loader import SkillLoader
from backend.skills.manager import SkillManager


@pytest.fixture
def isolated_plugins(tmp_path, monkeypatch):
    state = tmp_path / "state"
    settings = tmp_path / "settings.json"
    settings.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(config, "STATE_ROOT", state)
    monkeypatch.setattr(commands, "STATE_ROOT", state)
    monkeypatch.setattr("backend.skills.loader.STATE_ROOT", state)
    monkeypatch.setattr(config_helpers, "SETTINGS_FILE", settings)
    monkeypatch.setattr(service, "SETTINGS_FILE", settings)
    read = lambda: json.loads(settings.read_text(encoding="utf-8"))
    stack = lambda **kwargs: ConfigLayerStack((ConfigLayer(ConfigLayerSource("user", file=str(settings)), read()),))
    monkeypatch.setattr(config, "load_config_layer_stack", stack)
    monkeypatch.setattr(service, "_load_settings_json", read)
    monkeypatch.setattr(service, "feature_enabled", lambda *_: True)
    policy = ManagedPluginPolicy(enabled_plugins={}, strict_known_marketplaces=None,
        blocked_marketplaces=(), marketplace_requirements={})
    monkeypatch.setattr(service, "_plugin_policy_from_stack", lambda *_: policy)

    async def changed(**kwargs):
        pass

    async def install(root):
        return await service.import_plugin_from_path(root, settings_file=settings,
            config_change_hook=changed, _policy=policy)

    return stack, install


def write_plugin(tmp_path, name, *, legacy=False, extra=None):
    root = tmp_path / name
    manifest = root / (".minicode-plugin/plugin.json" if legacy else "plugin.json")
    manifest.parent.mkdir(parents=True)
    payload = {"name": name, "version": "1.0.0", **({} if legacy else {"$schema": AGENT_PLUGIN_SCHEMA}), **(extra or {})}
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    return root


def write_mcp(root, servers):
    (root / "mcp.json").write_text(json.dumps({"$schema": AGENT_PLUGIN_MCP_SCHEMA, "mcpServers": servers}), encoding="utf-8")


@pytest.mark.asyncio
@pytest.mark.parametrize("placement", ["unknown-top-level", "legacy-default-file", "com.openai-extension", "legacy-control"])
async def test_manifest_format_controls_real_hook_discovery_and_execution(tmp_path, isolated_plugins, placement):
    stack, install = isolated_plugins
    marker = "AGENT_PLUGIN_FORMAT_HOOK_MARKER"
    handler = {"type": "command", "command": "Write-Output " + marker, "shell": "powershell"} if os.name == "nt" else {
        "type": "command", "command": "echo " + marker, "shell": "bash"}
    hooks = {"session_start": [{"hooks": [handler]}]}
    extra = {"hooks": hooks} if placement in {"unknown-top-level", "legacy-control"} else {
        "extensions": {"com.openai": {"hooks": hooks}}} if placement == "com.openai-extension" else {}
    root = write_plugin(tmp_path, "hook-boundary", legacy=placement == "legacy-control", extra=extra)
    if placement == "legacy-default-file":
        (root / "hooks").mkdir()
        (root / "hooks/hooks.json").write_text(json.dumps({"hooks": hooks}), encoding="utf-8")
    installed = await install(root)
    entry = installed["plugins"][0]
    legacy = placement == "legacy-control"
    assert entry["hook_count"] == int(legacy)
    assert entry["runtime_support"]["hooks"] is legacy
    assert not service.load_enabled_plugin_extension_sources(config_stack=stack())
    sources = service.load_enabled_plugin_hook_sources(config_stack=stack())
    snapshot = discover_hook_snapshot(config_stack=stack(), workspace_root=tmp_path,
        workspace_trusted=True, plugin_sources=sources)
    assert len(snapshot.executable_entries) == int(legacy)
    manager = HookManager.from_snapshot(snapshot, workspace_root=tmp_path)
    result = await manager.run_session_start("agent-plugin-format-probe")
    assert (marker in json.dumps(result.__dict__, default=str)) is legacy


@pytest.mark.asyncio
async def test_canonical_plugin_install_catalog_stdio_call_and_data_context(tmp_path, isolated_plugins, monkeypatch):
    stack, install = isolated_plugins
    root = write_plugin(tmp_path, "canonical-peer")
    shutil.copyfile(Path(__file__).parent / "fixtures/agent_plugin_peer.py", root / "peer.py")
    write_mcp(root, {"worker": {"type": "stdio", "command": "python",
        "args": ["${PLUGIN_ROOT}/peer.py", "${PLUGIN_DATA}/argument"], "cwd": "${PLUGIN_DATA}",
        "env": {"LITERAL": "${PRESERVE_THIS_LITERAL}", "PLUGIN_TEST_TOKEN": "${PLUGIN_TEST_TOKEN}"}}})
    overlay = root / ".codex-plugin/plugin.json"
    overlay.parent.mkdir()
    overlay.write_text(json.dumps({"name": "canonical-peer", "mcpServers": {
        "worker": {"command": "unused-overlay-command", "env_vars": ["PLUGIN_TEST_TOKEN"]},
        "overlay-only": {"command": "must-not-launch", "env_vars": ["PLUGIN_TEST_TOKEN"]}}}), encoding="utf-8")
    monkeypatch.setenv("PLUGIN_TEST_TOKEN", "local-fixture-token")
    installed = await install(root)
    entry = installed["plugins"][0]
    assert entry["mcp_server_count"] == 1 and entry["mcp_server_names"] == ["worker"]
    manager = MCPServerManager(config_path=tmp_path / "absent-mcp.json", workspace_root=tmp_path, runtime_configs=())
    configs = manager._load_plugin_configs(config_stack=stack())
    assert len(configs) == 1
    server = configs[0]
    data = agent_plugin_data_root("canonical-peer@local").resolve()
    package = Path(installed["imported"]["path"]).resolve()
    assert server.name == "plugin:canonical-peer@local:worker" and server.command == "python"
    assert server.cwd == str(data) and server.env["PLUGIN_ROOT"] == str(package)
    assert server.env["LITERAL"] == "${PRESERVE_THIS_LITERAL}"
    assert server.env["PLUGIN_TEST_TOKEN"] == "local-fixture-token"
    assert data.is_dir() and server.config_path == str(package / "mcp.json")
    try:
        await manager.start_server(server)
        assert manager._servers[server.name].status is ServerStatus.CONNECTED
        status, = manager.get_all_status()
        assert (status["name"], status["plugin_id"], status["plugin_server_name"]) == (
            "plugin:canonical-peer@local:worker", "canonical-peer@local", "worker")
        client = manager.get_client(server.name)
        result = await client.call_tool("inspect_launch", {})
        launch = result.structured_content
        assert launch == {"cwd": str(data), "root": str(package), "data": str(data),
            "argument": str(data) + "/argument", "literal": "${PRESERVE_THIS_LITERAL}", "overlay": "local-fixture-token"}
        calls = [json.loads(line) for line in (data / "peer-calls.jsonl").read_text(encoding="utf-8").splitlines()]
        assert {call.get("method") for call in calls} >= {"initialize", "tools/list", "tools/call"}
    finally:
        await manager.stop_all()
    assert manager.get_client(server.name) is None


def test_agent_plugin_data_is_owned_by_full_identity_and_stable_across_versions(tmp_path, monkeypatch):
    import hashlib

    monkeypatch.setattr(config, "STATE_ROOT", tmp_path)
    first = agent_plugin_data_root("a-b@c")
    second = agent_plugin_data_root("a@b-c")
    assert first != second
    assert first.name == hashlib.sha256(b"c\0a-b").hexdigest()
    assert agent_plugin_data_root("a-b@c@>=1.0") == first
    assert first.parent.name == "agent-plugins"


def test_captured_root_manifest_cannot_change_format_by_removing_schema(tmp_path):
    root = write_plugin(tmp_path, "captured-root")
    manifest = root / "plugin.json"
    assert _load_plugin_manifest(manifest).format == "agent_plugin"
    manifest.write_text(json.dumps({"name": "captured-root", "hooks": {"SessionStart": []}, "skills": "./skills"}), encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported Agent Plugins schema"):
        _load_plugin_manifest(manifest)
    assert plugin_skill_sources(root, [manifest]) == ([], True)
    manifest.write_text(json.dumps({"$schema": "https://agent-plugins.org/schemas/999/plugin.schema.json", "name": "captured-root"}), encoding="utf-8")
    validation = validate_plugin_directory(root)
    assert validation["ok"] is False and any("unsupported Agent Plugins schema" in error for error in validation["errors"])


@pytest.mark.parametrize("directory", [".minicode-plugin", ".codex-plugin"])
def test_legacy_manifest_position_keeps_legacy_format_when_unknown_schema_is_present(tmp_path, directory):
    root = tmp_path / "legacy-position"
    manifest = root / directory / "plugin.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"$schema": AGENT_PLUGIN_SCHEMA, "name": "legacy-position", "hooks": {"SessionStart": []}}), encoding="utf-8")
    loaded = _load_plugin_manifest(manifest)
    assert loaded.format == "legacy" and "hooks" in loaded.payload


@pytest.mark.asyncio
async def test_canonical_root_is_the_single_manifest_for_inventory_execution_and_uninstall(tmp_path, isolated_plugins, monkeypatch):
    stack, install = isolated_plugins
    root = write_plugin(tmp_path, "root-wins")
    write_mcp(root, {"canonical": {"type": "stdio", "command": "not-launched"}})
    legacy = root / ".minicode-plugin/plugin.json"
    legacy.parent.mkdir()
    legacy.write_text(json.dumps({"name": "root-wins", "version": "1.0.0", "skills": "./extra",
        "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "must-not-execute"}]}]},
        "mcp_servers": {"stale-server": {"transport": "stdio", "command": "must-not-launch"}},
        "extensions": "./stale.py"}), encoding="utf-8")
    (root / "stale.py").write_text('raise RuntimeError("must-not-import")', encoding="utf-8")
    for relative, name in [("skills/direct", "direct"), ("extra/stale", "stale")]:
        skill = root / relative / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text(f"---\nname: {name}\ndescription: Local fixture workflow\n---\nActual fixture instructions.", encoding="utf-8")
    for discovery_root in (root.parent, root, root / "plugin.json", legacy.parent, legacy):
        assert commands._iter_plugin_manifests([discovery_root]) == [root / "plugin.json"]
    installed = await install(root)
    stored = Path(installed["imported"]["path"])
    entry, = installed["plugins"]
    assert entry["manifest_paths"] == [str(stored / "plugin.json")]
    assert (entry["skill_count"], entry["hook_count"], entry["extension_count"], entry["mcp_server_count"]) == (1, 0, 0, 1)
    assert entry["mcp_server_names"] == ["canonical"]
    assert not service.load_enabled_plugin_hook_sources(config_stack=stack())
    assert not service.load_enabled_plugin_extension_sources(config_stack=stack())
    monkeypatch.setattr(SkillLoader, "_search_dirs", lambda self: self._plugin_search_dirs())
    loader = SkillLoader()
    loader._config_stack = stack()
    skills = SkillManager(loader).snapshot(config_stack=stack())
    assert {skill.name for skill in skills.discover()} == {"root-wins@local:direct"}
    manager = MCPServerManager(config_path=tmp_path / "absent.json", workspace_root=None, runtime_configs=())
    assert [server.name for server in manager._load_plugin_configs(config_stack=stack())] == ["plugin:root-wins@local:canonical"]

    async def changed(**kwargs):
        pass

    removed = await service.remove_plugin("root-wins@local", settings_file=config_helpers.SETTINGS_FILE, config_change_hook=changed)
    assert removed["plugins"] == [] and not stored.exists()
    assert legacy.is_file() and (root / "stale.py").is_file()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["object", "path", "default", "invalid-path-fallback", "parent-component-fallback", "empty-relative-fallback", "snake-unknown", "invalid-name", "invalid-interface"])
async def test_codex_env_overlay_uses_canonical_manifest_fields_and_typed_admission(tmp_path, isolated_plugins, monkeypatch, mode):
    stack, install = isolated_plugins
    root = write_plugin(tmp_path, "overlay-probe")
    write_mcp(root, {"worker": {"type": "stdio", "command": "original-command",
        "env": {"PLUGIN_TEST_TOKEN": "${PLUGIN_TEST_TOKEN}"}}})
    servers = {"worker": {"command": "overlay-command-must-not-replace-original",
        "env_vars": ["PLUGIN_TEST_TOKEN", {"name": "REMOTE_ONLY", "source": "remote"}]}}
    manifest = {"name": "overlay-probe"}
    if mode in {"object", "invalid-name", "invalid-interface"}:
        manifest["mcpServers"] = servers
    elif mode == "snake-unknown":
        manifest["mcp_servers"] = servers
    else:
        path = root / ("overlay-config.json" if mode == "path" else ".mcp.json")
        path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
        if mode == "path":
            manifest["mcpServers"] = "./overlay-config.json"
        elif mode == "invalid-path-fallback":
            manifest["mcpServers"] = "../outside.json"
        elif mode == "parent-component-fallback":
            manifest["mcpServers"] = "./sub/../different.json"
            (root / "sub").mkdir()
            (root / "different.json").write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
        elif mode == "empty-relative-fallback":
            manifest["mcpServers"] = "./"
    if mode == "invalid-name":
        manifest["name"] = 42
    elif mode == "invalid-interface":
        manifest["interface"] = {"displayName": 42}
    overlay = root / ".codex-plugin/plugin.json"
    overlay.parent.mkdir()
    overlay.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setenv("PLUGIN_TEST_TOKEN", "overlay-host-value")
    await install(root)
    manager = MCPServerManager(config_path=tmp_path / "absent.json", workspace_root=None, runtime_configs=())
    server, = manager._load_plugin_configs(config_stack=stack())
    applied = mode in {"object", "path", "default", "invalid-path-fallback", "parent-component-fallback", "empty-relative-fallback"}
    assert server.command == "original-command"
    assert server.env["PLUGIN_TEST_TOKEN"] == ("overlay-host-value" if applied else "${PLUGIN_TEST_TOKEN}")
    assert "REMOTE_ONLY" not in server.env


@pytest.mark.asyncio
async def test_invalid_plugin_mcp_and_invalid_sibling_do_not_discard_valid_catalog(tmp_path, isolated_plugins, caplog):
    stack, install = isolated_plugins
    malformed = write_plugin(tmp_path, "malformed-config")
    (malformed / "mcp.json").write_text('{"$schema": "wrong", "mcpServers": {}}', encoding="utf-8")
    await install(malformed)
    root = write_plugin(tmp_path, "mixed-config")
    write_mcp(root, {"bad": {"type": "stdio", "command": "../escape"},
        " good ": {"type": "streamable-http", "url": "https://example.invalid/mcp", "headers": {"X-Fixture": "value", "Authorization": "not-forwarded"}}})
    installed = await install(root)
    mixed = next(item for item in installed["plugins"] if item["name"] == "mixed-config")
    assert mixed["mcp_server_names"] == [" good "] and mixed["mcp_server_count"] == 1
    local_path = tmp_path / "local-mcp.json"
    local_path.write_text(json.dumps({"servers": {"local": {"transport": "stdio", "command": "not-launched"}}}), encoding="utf-8")
    manager = MCPServerManager(config_path=local_path, workspace_root=None, managed_settings_dir=tmp_path / "managed")
    catalog = manager.load_config()
    assert {server.name for server in catalog} == {"plugin:mixed-config@local: good ", "local"}
    remote = next(server for server in catalog if server.name.endswith(": good "))
    assert remote.transport == "http" and remote.headers == {"X-Fixture": "value"}
    assert "unsupported Agent Plugins MCP schema" in caplog.text and "../escape" not in {server.command for server in catalog}


@pytest.mark.parametrize("host", ["127.0.0.1", "127.1", "2130706433", "0x7f000001", "0177.0.0.1", "localhost", "[::1]"])
def test_source_numeric_loopback_url_forms_are_accepted_without_connecting(tmp_path, host):
    outcome = parse_agent_plugin_mcp_config(tmp_path, tmp_path / "data", json.dumps({"$schema": AGENT_PLUGIN_MCP_SCHEMA,
        "mcpServers": {"local": {"type": "streamable-http", "url": f"http://{host}/mcp"}}}))
    assert set(outcome.servers) == {"local"} and not outcome.errors


@pytest.mark.parametrize("server", [
    {"type": "stdio", "command": "./../escape"},
    {"type": "stdio", "command": "python", "cwd": "${PLUGIN_DATA}/../escape"},
    {"type": "stdio", "command": "python", "env": {"PLUGIN_ROOT": "override"}},
    {"type": "stdio", "command": "python", "cwd": None},
    {"type": "stdio", "command": "python", "unknown": True},
    {"type": "streamable-http", "url": "http://example.invalid/mcp"},
    {"type": "streamable-http", "url": "https://example.invalid/#"},
    {"type": "streamable-http", "url": "https://user@example.invalid/"},
    {"type": "streamable-http", "url": "https://example.invalid/", "headers": {"X-A": "a", "x-a": "b"}},
    {"type": "streamable-http", "url": "https://example.invalid/", "headers": None},
    {"type": "sse", "url": "https://example.invalid/"},
    {"type": []},
])
def test_invalid_source_contract_entries_are_isolated_from_valid_siblings(tmp_path, server):
    outcome = parse_agent_plugin_mcp_config(tmp_path, tmp_path / "data", json.dumps({"$schema": AGENT_PLUGIN_MCP_SCHEMA,
        "mcpServers": {"bad": server, "good": {"type": "stdio", "command": "python"}}}))
    assert set(outcome.servers) == {"good"} and set(outcome.errors) == {"bad"}


def test_placeholders_are_single_pass_and_only_ascii_environment_case_is_folded(tmp_path):
    root = tmp_path / "literal-${PLUGIN_DATA}"
    root.mkdir()
    outcome = parse_agent_plugin_mcp_config(root, tmp_path / "data", json.dumps({"$schema": AGENT_PLUGIN_MCP_SCHEMA,
        "mcpServers": {"worker": {"type": "stdio", "command": "python", "args": ["${PLUGIN_ROOT}/${UNRECOGNIZED}"],
            "env": {"ß": "one", "ss": "two"}}}}))
    server = outcome.servers["worker"]
    assert server["args"] == [str(root.resolve()) + "/${UNRECOGNIZED}"]
    assert server["env"]["ß"] == "one" and server["env"]["SS" if os.name == "nt" else "ss"] == "two"


def test_duplicate_root_fields_are_rejected_but_value_map_keys_use_source_last_value(tmp_path):
    with pytest.raises(ValueError, match="duplicate"):
        parse_agent_plugin_mcp_config(tmp_path, tmp_path / "data",
            '{"$schema":' + json.dumps(AGENT_PLUGIN_MCP_SCHEMA) + ',"$schema":' + json.dumps(AGENT_PLUGIN_MCP_SCHEMA) + ',"mcpServers":{}}')
    outcome = parse_agent_plugin_mcp_config(tmp_path, tmp_path / "data",
        '{"$schema":' + json.dumps(AGENT_PLUGIN_MCP_SCHEMA) + ',"mcpServers":{'
        '"duplicate":{"type":"stdio","command":"first","command":"last"},'
        '"valid":{"type":"stdio","command":"python","env":{"SAME":"first","SAME":"last"}}}}')
    assert not outcome.errors and outcome.servers["duplicate"]["command"] == "last"
    assert outcome.servers["valid"]["env"]["SAME"] == "last"
