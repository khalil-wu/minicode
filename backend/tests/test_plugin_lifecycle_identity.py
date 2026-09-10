from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import pytest

import backend.commands.plugins as plugin_commands
import backend.config as config
import backend.services.plugin_settings_service as service
from backend.plugins.layout import plugin_install_root
from backend.plugins.policy import ManagedPluginPolicy
from backend.plugins.store import PluginStore


@pytest.fixture
def plugin_home(tmp_path, monkeypatch):
    state = tmp_path / "state"
    settings = tmp_path / "settings.json"
    settings.write_text("{}")
    monkeypatch.setattr(config, "STATE_ROOT", state)
    monkeypatch.setattr(plugin_commands, "STATE_ROOT", state)
    monkeypatch.setattr(service, "SETTINGS_FILE", settings)
    class EmptyStack:
        def effective_config(self):
            return {}
    monkeypatch.setattr(config, "load_config_layer_stack", lambda: EmptyStack())
    policy = ManagedPluginPolicy(enabled_plugins={}, strict_known_marketplaces=None, blocked_marketplaces=(), marketplace_requirements={})
    monkeypatch.setattr(service, "_plugin_policy_from_stack", lambda *_args: policy)
    source = tmp_path / "source"
    manifest = source / ".minicode-plugin/plugin.json"
    manifest.parent.mkdir(parents=True)
    assets = source / "assets"
    assets.mkdir()
    (assets / "light.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
    (assets / "dark.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
    manifest.write_text(json.dumps({"name": "review", "version": "1.0.0", "skills": "./skills", "interface": {"logo": "./assets/light.svg", "logoDark": "./assets/dark.svg"}}))
    skill = source / "skills/check/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: check\ndescription: Review code\n---\nReview code.\n")
    return source, settings


async def allow_change(**_kwargs):
    pass


def install(home, marketplace):
    source, settings = home
    return asyncio.run(service.import_plugin_from_path(source, marketplace=marketplace, settings_file=settings, config_change_hook=allow_change))


def test_catalog_install_has_one_identity_and_enablement_does_not_cross_markets(plugin_home):
    first = install(plugin_home, "team-a")
    assert [plugin["id"] for plugin in first["plugins"]] == ["review@team-a"]
    assert first["plugins"][0]["iconVariants"] == ["logo", "logo-dark"]
    assert Path(first["imported"]["path"]) == PluginStore().version_path("team-a", "review", "1.0.0")
    install(plugin_home, "team-b")
    _, settings = plugin_home
    asyncio.run(service.update_plugin_enabled("review@team-a", False, settings_file=settings, config_change_hook=allow_change))
    states = {plugin["id"]: plugin["enabled"] for plugin in service.get_plugin_settings()["plugins"]}
    assert states == {"review@team-a": False, "review@team-b": True}
    # A selection for one marketplace cannot implicitly activate another.
    service._write_settings_json({"plugins": {"review@team-a": {"enabled": True}}})
    states = {plugin["id"]: plugin["enabled"] for plugin in service.get_plugin_settings()["plugins"]}
    assert states == {"review@team-a": True, "review@team-b": False}


@pytest.mark.parametrize("legacy", [False, True])
def test_uninstall_removes_store_and_all_compatibility_files(plugin_home, legacy):
    result = install(plugin_home, "team-a")
    projection = Path(result["imported"]["path"])
    if legacy:
        old_projection = plugin_install_root() / "review-team-a"
        shutil.copytree(projection, old_projection)
        projection = old_projection
        assert [plugin["id"] for plugin in service.get_plugin_settings()["plugins"]] == ["review@team-a"]
    _, settings = plugin_home
    removed = asyncio.run(service.remove_plugin("review@team-a", settings_file=settings, config_change_hook=allow_change))
    assert removed["plugins"] == []
    assert not projection.exists()
    assert PluginStore().list() == []
    assert not service._load_settings_json().get("plugins")


def test_uninstall_failure_restores_both_storage_locations(plugin_home, monkeypatch):
    result = install(plugin_home, "team-a")
    projection = Path(result["imported"]["path"])
    store_path = Path(result["imported"]["store"]["path"])
    def fail_save(_data):
        raise OSError("settings write failed")
    monkeypatch.setattr(service, "_write_settings_json", fail_save)
    with pytest.raises(OSError, match="settings write failed"):
        asyncio.run(service.remove_plugin("review@team-a", settings_file=plugin_home[1], config_change_hook=allow_change))
    assert projection.is_dir()
    assert store_path.is_dir()
    assert [plugin["id"] for plugin in service.get_plugin_settings()["plugins"]] == ["review@team-a"]
