from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import config, config_helpers, config_layers
from backend.api import _state, routes_skills
from backend.commands import plugins as commands
from backend.mcp.manager import MCPServerManager
from backend.plugins import package
from backend.plugins.manager import MarketplaceRegistry
from backend.services import plugin_settings_service as plugins
from backend.services.computer_use_controls import computer_use_plugin_controls


@pytest.fixture
def catalog_home(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    settings = state / "settings.json"
    settings.write_text(json.dumps({"feature_flags": {"plugins": True}, "unrelated": "keep"}), encoding="utf8")
    monkeypatch.setattr(config, "STATE_ROOT", state)
    monkeypatch.setattr(config_helpers, "STATE_ROOT", state)
    monkeypatch.setattr(config_helpers, "SETTINGS_FILE", settings)
    monkeypatch.setattr(commands, "STATE_ROOT", state)
    monkeypatch.setattr(plugins, "SETTINGS_FILE", settings)
    monkeypatch.setattr(routes_skills, "SETTINGS_FILE", settings)
    monkeypatch.setenv("MINICODE_PLUGIN_REGISTRY", str(state / "extensions/plugins/marketplaces.json"))
    monkeypatch.setenv("MINICODE_REQUIREMENTS_FILE", str(tmp_path / "missing-requirements.toml"))
    monkeypatch.delenv("MINICODE_PROFILE", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.setattr(config_layers, "default_system_config_path", lambda: tmp_path / "missing-system.toml")
    monkeypatch.setattr("backend.managed_settings.load_minicode_managed_settings", lambda *args, **kwargs: SimpleNamespace(configured=False, validation_errors=()))
    monkeypatch.setattr("backend.workspace.trust.is_workspace_trusted", lambda root: True)
    monkeypatch.setattr("backend.workspace.state.get_explicit_active_workspace_root", lambda: None)
    monkeypatch.setattr(plugins, "feature_enabled", lambda *_: True)
    monkeypatch.setattr(package, "feature_enabled", lambda *_: True)
    monkeypatch.setattr(_state, "bootstrap", None)
    monkeypatch.setattr(_state, "invalidate_status_cache", lambda: None)

    async def allowed(**_kwargs):
        return None

    monkeypatch.setattr(routes_skills, "run_config_change_hook", allowed)
    app = FastAPI()
    app.include_router(routes_skills.router)
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, state=state, settings=settings, root=tmp_path)


def write_marketplace(root: Path, name: str = "project-market") -> Path:
    catalog = root / ".agents/plugins/marketplace.json"
    catalog.parent.mkdir(parents=True)
    catalog.write_text(json.dumps({"name": name, "plugins": [{"name": "review", "category": "Developer Tools",
        "source": {"source": "local", "path": "./plugins/review"}}]}), encoding="utf8")
    plugin = root / "plugins/review"
    manifest = plugin / ".codex-plugin/plugin.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"name": "review", "version": "1.0.0", "description": "Actual review package",
        "skills": "./skills", "mcpServers": "./.mcp.json", "interface": {"displayName": "Real Review",
        "shortDescription": "Review the project", "logo": "./logo.svg"}}), encoding="utf8")
    (plugin / ".mcp.json").write_text(json.dumps({"mcpServers": {"reviewer": {"command": "review-server"}}}), encoding="utf8")
    (plugin / "logo.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>', encoding="utf8")
    skill = plugin / "skills/review/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: review\ndescription: Review real changes\n---\nInspect changes.\n", encoding="utf8")
    return plugin


def test_bundled_catalog_install_runtime_and_feature_switch(catalog_home):
    env = catalog_home
    client = env.client
    response = client.get("/api/plugins/marketplaces", params={"workspace_root": ""})
    assert response.status_code == 200, response.text
    marketplace = next(item for item in response.json()["marketplaces"] if item["name"] == "minicode-bundled")
    assert marketplace["directory"] == "public" and marketplace["isBuiltIn"]
    plugin = marketplace["plugins"][0]
    assert plugin["id"] == "computer-use@minicode-bundled"
    assert plugin["interface"]["displayName"] == "Computer Use"
    assert plugin["mcp_server_names"] == ["cua_repl"]
    assert [skill["name"] for skill in plugin["skill_summaries"]] == ["computer-use"]
    assert client.get("/api/plugins", params={"workspace_root": ""}).json()["plugins"] == []
    controls, _, _, _ = computer_use_plugin_controls(None)
    assert controls[0]["plugin_id"] == plugin["id"] and controls[0]["can_install"]

    installed = client.post("/api/plugins/install", json={"marketplace": marketplace["name"], "plugin_name": plugin["name"],
        "refresh_marketplace": False, "workspace_root": ""})
    assert installed.status_code == 200, installed.text
    inventory = client.get("/api/plugins", params={"workspace_root": ""}).json()
    assert [(item["id"], item["enabled"], item["skill_count"]) for item in inventory["plugins"]] == [(plugin["id"], True, 1)]
    controls, _, _, _ = computer_use_plugin_controls(None)
    assert controls[0]["installed"] and controls[0]["enabled"] and controls[0]["can_toggle"]
    assert computer_use_plugin_controls(None, "other@market")[0] == []
    assert computer_use_plugin_controls(None, plugin["id"])[0][0]["plugin_id"] == plugin["id"]
    stack = config.load_config_layer_stack(cwd=None)
    mcp = MCPServerManager(config_path=env.root / "mcp.json", workspace_root=None)
    declarations = mcp._load_plugin_configs(config_stack=stack)
    assert [(item.name, item.plugin_id, item.plugin_server_name) for item in declarations] == [("cua_repl", plugin["id"], "cua_repl")]

    settings = json.loads(env.settings.read_text(encoding="utf8"))
    settings["feature_flags"]["plugins"] = False
    env.settings.write_text(json.dumps(settings), encoding="utf8")
    assert client.get("/api/plugins/marketplaces", params={"workspace_root": ""}).json()["marketplaces"] == []
    disabled = client.get("/api/plugins", params={"workspace_root": ""}).json()
    assert not disabled["plugins_enabled"] and disabled["plugins"][0]["enabled"]
    denied = client.post("/api/plugins/install", json={"marketplace": marketplace["name"], "plugin_name": plugin["name"], "workspace_root": ""})
    assert denied.status_code == 409
    assert json.loads(env.settings.read_text(encoding="utf8"))["unrelated"] == "keep"


def test_workspace_catalog_manifest_assets_install_and_project_override(catalog_home):
    env = catalog_home
    project = env.root / "project"
    (project / ".git").mkdir(parents=True)
    other = env.root / "other"
    (other / ".git").mkdir(parents=True)
    source = write_marketplace(project)
    extra = env.root / "project-configured"
    write_marketplace(extra, "configured-project")
    config_file = project / ".minicode/config.toml"
    config_file.parent.mkdir()
    config_file.write_text('[plugins."review@project-market"]\nenabled = false\n'
        f'[marketplaces."configured-project"]\nsource_type = "local"\nsource = "{extra.as_posix()}"\n', encoding="utf8")
    client = env.client
    list_for = lambda root: client.get("/api/plugins/marketplaces", params={"workspace_root": str(root)}).json()["marketplaces"]
    record = next(item for item in list_for(project) if item["name"] == "project-market")
    entry = record["plugins"][0]
    assert record["directory"] == "personal" and record["isConfigured"] is False
    assert entry["interface"]["displayName"] == "Real Review"
    assert entry["mcp_server_names"] == ["reviewer"]
    assert entry["skill_summaries"][0]["description"] == "Review real changes"
    assert not any(item["name"] == "project-market" for item in list_for(other))
    assert any(item["name"] == "configured-project" and item["isConfigured"] for item in list_for(project))
    assert not any(item["name"] == "configured-project" for item in list_for(other))
    asset = {"plugin_path": str(source), "variant": "logo"}
    assert client.get("/api/plugins/asset", params={**asset, "workspace_root": str(project)}).status_code == 200
    assert client.get("/api/plugins/asset", params={**asset, "workspace_root": str(other)}).status_code == 404

    installed = client.post("/api/plugins/install", json={"marketplace": record["name"], "plugin_name": "review",
        "refresh_marketplace": False, "workspace_root": str(project)})
    assert installed.status_code == 200, installed.text
    item_a = client.get("/api/plugins", params={"workspace_root": str(project)}).json()["plugins"][0]
    item_b = client.get("/api/plugins", params={"workspace_root": str(other)}).json()["plugins"][0]
    assert not item_a["enabled"] and not item_a["user_config_writable"]
    assert item_b["enabled"] and item_b["user_config_writable"]
    assert item_b["mcp_server_names"] == ["reviewer"]
    mcp = MCPServerManager(config_path=env.root / "mcp.json", workspace_root=other)
    declarations = mcp._load_plugin_configs(config_stack=config.load_config_layer_stack(cwd=other))
    assert [(item.name, item.command) for item in declarations] == [("plugin:review@project-market:reviewer", "review-server")]
    original = env.settings.read_bytes()
    denied = client.put("/api/plugins/review%40project-market/state", json={"enabled": True, "workspace_root": str(project)})
    assert denied.status_code == 409 and env.settings.read_bytes() == original
    assert 'enabled = false\n' in config_file.read_text(encoding="utf8")


def test_configured_marketplace_and_registered_snapshot_are_read_from_current_sources(catalog_home):
    env = catalog_home
    source = env.root / "source"
    write_marketplace(source, "configured")
    settings = json.loads(env.settings.read_text(encoding="utf8"))
    settings["marketplaces"] = {"configured": {"source_type": "local", "source": str(source)}}
    env.settings.write_text(json.dumps(settings), encoding="utf8")
    record = next(item for item in env.client.get("/api/plugins/marketplaces", params={"workspace_root": ""}).json()["marketplaces"] if item["name"] == "configured")
    assert record["isConfigured"] and not record["canManage"]
    assert record["plugins"][0]["interface"]["displayName"] == "Real Review"

    registered = env.root / "registered"
    write_marketplace(registered, "registered")
    added = MarketplaceRegistry().add_source(str(registered))[0]
    manifest = Path(added["materialized_path"]) / "plugins/review/.codex-plugin/plugin.json"
    payload = json.loads(manifest.read_text(encoding="utf8"))
    payload["interface"]["displayName"] = "Updated Review"
    manifest.write_text(json.dumps(payload), encoding="utf8")
    listing = env.client.get("/api/plugins/marketplaces", params={"workspace_root": ""}).json()["marketplaces"]
    assert next(item for item in listing if item["name"] == "registered")["plugins"][0]["interface"]["displayName"] == "Updated Review"

    Path(added["materialized_path"], ".agents/plugins/marketplace.json").write_text("{broken", encoding="utf8")
    listing = env.client.get("/api/plugins/marketplaces", params={"workspace_root": ""}).json()["marketplaces"]
    failed = next(item for item in listing if item["name"] == "registered")
    assert failed["status"] == "error" and failed["error"] and failed["plugins"] == []
