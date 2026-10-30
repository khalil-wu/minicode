"""Real local plugin install/enablement through the Computer Use consumer."""
from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.api import _state, routes_skills
from backend.mcp.manager import MCPServerManager
from backend.plugins.manager import MarketplaceRegistry, PluginManager
from backend.services import computer_use_settings, plugin_settings_service
from backend.skills.loader import SkillLoader
from backend.skills.manager import SkillManager
from backend.tests.test_computer_use_settings import native_config, settings_client


PLUGIN_ID = "computer-use@openai-bundled"


@pytest.fixture
def control_scope(tmp_path, monkeypatch):
    client, query, session, _ = settings_client(monkeypatch, tmp_path, native_config())
    client._transport.app.include_router(routes_skills.router)
    state = Path(os.environ["MINICODE_STATE_ROOT"])
    monkeypatch.setattr("backend.skills.loader.STATE_ROOT", state)
    settings = state / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text('{"feature_flags":{"plugins":true}}', encoding="utf8")
    monkeypatch.setattr(plugin_settings_service, "SETTINGS_FILE", settings)
    monkeypatch.setattr(routes_skills, "SETTINGS_FILE", settings)
    config = state / "native-mcp.json"
    selected = native_config()
    config.write_text(json.dumps({"servers": {"cua_repl": {"transport": "stdio", "command": selected.command, "env": selected.env}}}), encoding="utf8")
    manager = MCPServerManager(config_path=config, workspace_root=None)
    session._mcp_manager_for_workspace = lambda root: manager
    skills = SkillManager(SkillLoader())
    monkeypatch.setattr(_state, "bootstrap", SimpleNamespace(skill_manager=skills, mcp_manager=manager, config=None))
    hooks = []
    async def allowed_change(**kwargs):
        hooks.append(kwargs)
    monkeypatch.setattr(routes_skills, "run_config_change_hook", allowed_change)
    return SimpleNamespace(client=client, query=query, session=session, settings=settings,
        manager=manager, skills=skills, hooks=hooks, root=tmp_path)


def register_catalog(root, *, marketplace="openai-bundled", policy=None, include_cua=False, assets=False):
    source = root / marketplace
    plugin = source / "plugins/computer-use"
    manifest = plugin / ".minicode-plugin/plugin.json"
    manifest.parent.mkdir(parents=True)
    metadata = {"name": "computer-use", "version": "1.0.0", "skills": "skills",
        "interface": {"displayName": "Actual Computer Control"}}
    if assets:
        (plugin / "assets").mkdir()
        for variant in ("logo", "dark"):
            (plugin / f"assets/{variant}.svg").write_text(f'<svg xmlns="http://www.w3.org/2000/svg"><title>{variant}</title></svg>', encoding="utf8")
        metadata["interface"].update(logo="assets/logo.svg", logoDark="assets/dark.svg")
    if include_cua:
        selected = native_config()
        metadata["mcp_servers"] = {"cua_repl": {"transport": "stdio", "command": selected.command, "env": selected.env}}
    manifest.write_text(json.dumps(metadata), encoding="utf8")
    skill = plugin / "skills/control/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: control\ndescription: A controlled local computer workflow\n---\nRead the selected target.\n", encoding="utf8")
    catalog = source / ".agents/plugins/marketplace.json"
    catalog.parent.mkdir(parents=True)
    catalog.write_text(json.dumps({"name": marketplace, "plugins": [{"name": "computer-use",
        "interface": {"displayName": "Actual Computer Control"},
        "source": {"source": "local", "path": "./plugins/computer-use"}, "policy": policy or {}}]}), encoding="utf8")
    return MarketplaceRegistry().add_source(str(source))[0]


def action(scope, snapshot, kind, **fields):
    return {**scope.query, "provider_id": snapshot["provider_id"], "selection_id": snapshot["selection_id"],
        "control_id": "any-app", "action": kind, **fields}


async def read(scope):
    response = await scope.client.get("/api/settings/computer-use", params=scope.query)
    assert response.status_code == 200, response.text
    return response.json()


def has_plugin_skill(scope):
    return any(skill.name == f"{PLUGIN_ID}:control" for skill in scope.skills.list_metas())


@pytest.mark.asyncio
async def test_exact_source_identity_requires_real_registered_catalog(control_scope):
    scope = control_scope
    async with scope.client:
        assert (await read(scope))["controls"][0]["plugin_id"] == "computer-use@minicode-bundled"
        register_catalog(scope.root, marketplace="other-market")
        assert (await read(scope))["controls"][0]["plugin_id"] == "computer-use@minicode-bundled"
        register_catalog(scope.root)
        snapshot = await read(scope)
        assert snapshot["controls"] == [{"kind": "plugin", "id": "any-app", "plugin_id": PLUGIN_ID,
            "plugin_name": "computer-use", "marketplace": "openai-bundled", "display_name": "Actual Computer Control",
            "installed": False, "enabled": False, "can_install": True, "can_toggle": False, "icon_url": None, "load_errors": [],
            "plugin_path": None, "icon_variant": None, "icon_variants": []}]
        assert PluginManager().snapshot().plugins == ()


@pytest.mark.asyncio
async def test_actual_install_disable_enable_refreshes_plugin_consumers_and_keeps_independent_provider(control_scope):
    scope = control_scope
    register_catalog(scope.root)
    async with scope.client:
        snapshot = await read(scope)
        installed = await scope.client.post("/api/settings/computer-use/controls/plugin", json=action(scope, snapshot, "install"))
        assert installed.status_code == 200, installed.text
        snapshot = installed.json()
        assert snapshot["runtime_refresh"]["ok"] is True
        assert set(snapshot["runtime_refresh"]["refreshed"]) == {"bootstrap.skills", "bootstrap.mcp"}
        assert snapshot["controls"][0]["installed"] and snapshot["controls"][0]["enabled"]
        assert snapshot["controls"][0]["can_toggle"] and not snapshot["controls"][0]["can_install"]
        assert has_plugin_skill(scope)
        assert Path(PluginManager().snapshot().plugins[0]["path"]).is_dir()
        for enabled in (False, True):
            previous = snapshot
            changed = await scope.client.post("/api/settings/computer-use/controls/plugin",
                json=action(scope, snapshot, "set_enabled", enabled=enabled))
            assert changed.status_code == 200, changed.text
            snapshot = changed.json()
            assert snapshot["runtime_refresh"]["ok"]
            assert snapshot["navigation_visible"] and snapshot["available"]
            assert snapshot["controls"][0]["installed"] and snapshot["controls"][0]["enabled"] is enabled
            assert has_plugin_skill(scope) is enabled
            assert [item.name for item in scope.manager.load_config()] == ["cua_repl"]
            assert snapshot["selection_id"] != previous["selection_id"]
        assert len(scope.hooks) == 3 and all(item["source"] == "plugins" for item in scope.hooks)
        assert json.loads(scope.settings.read_text())["plugins"][PLUGIN_ID]["enabled"] is True
    await scope.manager.stop_all()


@pytest.mark.asyncio
async def test_stale_catalog_and_owner_cannot_install_a_removed_or_foreign_control(control_scope):
    scope = control_scope
    register_catalog(scope.root)
    async with scope.client:
        snapshot = await read(scope)
        scope.session.active_conversation_id = "foreign"
        denied = await scope.client.post("/api/settings/computer-use/controls/plugin", json=action(scope, snapshot, "install"))
        assert denied.status_code == 409
        scope.session.active_conversation_id = "real-thread"
        MarketplaceRegistry().remove("openai-bundled")
        denied = await scope.client.post("/api/settings/computer-use/controls/plugin", json=action(scope, snapshot, "install"))
        assert denied.status_code == 409 and scope.hooks == []
        assert PluginManager().snapshot().plugins == ()


@pytest.mark.asyncio
async def test_plugin_only_declared_provider_remains_manageable_while_plugin_is_disabled(control_scope):
    scope = control_scope
    register_catalog(scope.root, include_cua=True)
    async with scope.client:
        snapshot = await read(scope)
        installed = await scope.client.post("/api/settings/computer-use/controls/plugin", json=action(scope, snapshot, "install"))
        assert installed.status_code == 200, installed.text
        scope.manager._config_path.write_text('{"servers":{}}')
        snapshot = await read(scope)
        actual = scope.manager.load_config()
        assert len(actual) == 1 and actual[0].plugin_id == PLUGIN_ID and actual[0].plugin_server_name == "cua_repl"
        for enabled in (False, True):
            changed = await scope.client.post("/api/settings/computer-use/controls/plugin",
                json=action(scope, snapshot, "set_enabled", enabled=enabled))
            assert changed.status_code == 200, changed.text
            snapshot = changed.json()
            assert snapshot["available"] and snapshot["navigation_visible"]
            assert snapshot["controls"][0]["installed"] and snapshot["controls"][0]["enabled"] is enabled
            assert bool(scope.manager.load_config()) is enabled
            assert not any(state.client for state in scope.manager._servers.values())
    await scope.manager.stop_all()


@pytest.mark.asyncio
async def test_enable_action_requires_its_explicit_boolean_without_writing_config(control_scope):
    scope = control_scope
    register_catalog(scope.root)
    async with scope.client:
        snapshot = await read(scope)
        original = scope.settings.read_bytes()
        invalid = await scope.client.post("/api/settings/computer-use/controls/plugin",
            json=action(scope, snapshot, "set_enabled"))
        assert invalid.status_code == 422
        assert scope.settings.read_bytes() == original and scope.hooks == []


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", [{"installation": "NOT_AVAILABLE"}, {"products": []}])
async def test_real_marketplace_policy_keeps_uninstallable_catalog_details_without_install_action(control_scope, policy):
    scope = control_scope
    register_catalog(scope.root, policy=policy)
    async with scope.client:
        snapshot = await read(scope)
        control = snapshot["controls"][0]
        assert not control["can_install"] and control["load_errors"]
        denied = await scope.client.post("/api/settings/computer-use/controls/plugin", json=action(scope, snapshot, "install"))
        assert denied.status_code == 409 and scope.hooks == []


@pytest.mark.asyncio
async def test_actual_runtime_refresh_failure_is_returned_after_config_install(control_scope, monkeypatch):
    scope = control_scope
    register_catalog(scope.root)
    def fail_discovery():
        raise OSError("original skill discovery failed")
    monkeypatch.setattr(scope.skills, "discover", fail_discovery)
    async with scope.client:
        snapshot = await read(scope)
        installed = await scope.client.post("/api/settings/computer-use/controls/plugin", json=action(scope, snapshot, "install"))
        assert installed.status_code == 200, installed.text
        result = installed.json()
        assert result["controls"][0]["installed"] and result["controls"][0]["enabled"]
        assert result["runtime_refresh"]["ok"] is False
        assert result["runtime_refresh"]["errors"] == ["Bootstrap skill refresh failed: original skill discovery failed"]
    await scope.manager.stop_all()


@pytest.mark.asyncio
async def test_installed_plugin_icon_metadata_uses_the_existing_real_asset_endpoint(control_scope):
    scope = control_scope
    register_catalog(scope.root, assets=True)
    async with scope.client:
        snapshot = await read(scope)
        installed = await scope.client.post("/api/settings/computer-use/controls/plugin", json=action(scope, snapshot, "install"))
        assert installed.status_code == 200, installed.text
        control = installed.json()["controls"][0]
        actual = PluginManager().snapshot().plugins[0]
        assert control["plugin_path"] == actual["path"] and control["icon_variant"] == "logo"
        assert control["icon_variants"] == ["logo", "logo-dark"]
        response = await scope.client.get("/api/plugins/asset", params={"plugin_path": control["plugin_path"], "variant": "logo-dark"})
        assert response.status_code == 200 and response.content == Path(actual["path"], "assets/dark.svg").read_bytes()
    await scope.manager.stop_all()


@pytest.mark.asyncio
async def test_project_override_keeps_actual_disabled_state_and_prevents_ineffective_user_toggle(control_scope, monkeypatch):
    scope = control_scope
    register_catalog(scope.root)
    (scope.root / ".git").mkdir()
    project = scope.root / ".minicode/config.toml"
    project.parent.mkdir()
    project.write_text(f'[plugins."{PLUGIN_ID}"]\nenabled = false\n', encoding="utf8")
    monkeypatch.setattr("backend.workspace.trust.is_workspace_trusted", lambda root: True)
    async with scope.client:
        snapshot = await read(scope)
        installed = await scope.client.post("/api/settings/computer-use/controls/plugin", json=action(scope, snapshot, "install"))
        assert installed.status_code == 200, installed.text
        current = installed.json()
        assert current["controls"][0]["installed"]
        assert not current["controls"][0]["enabled"] and not current["controls"][0]["can_toggle"]
        assert json.loads(scope.settings.read_text())["plugins"][PLUGIN_ID]["enabled"] is True
        original = scope.settings.read_bytes()
        denied = await scope.client.post("/api/settings/computer-use/controls/plugin",
            json=action(scope, current, "set_enabled", enabled=True))
        assert denied.status_code == 409 and scope.settings.read_bytes() == original
        assert len(scope.hooks) == 1
    await scope.manager.stop_all()


@pytest.mark.asyncio
async def test_no_original_provider_or_plugins_feature_does_not_retain_old_controls(control_scope):
    scope = control_scope
    register_catalog(scope.root)
    async with scope.client:
        assert (await read(scope))["controls"]
        settings = json.loads(scope.settings.read_text())
        settings["feature_flags"]["plugins"] = False
        scope.settings.write_text(json.dumps(settings))
        assert (await read(scope))["controls"] == []
        settings["feature_flags"]["plugins"] = True
        scope.settings.write_text(json.dumps(settings))
        scope.manager._config_path.write_text('{"servers":{}}')
        missing = await read(scope)
        assert missing["status"] == "not_configured" and missing["controls"] == []
        assert not missing["navigation_visible"]
