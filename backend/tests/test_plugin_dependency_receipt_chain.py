import json

import pytest

from backend import config
from backend.commands import plugins as plugin_commands
from backend.plugins.policy import ManagedPluginPolicy
from backend.services import plugin_settings_service as plugins


@pytest.mark.asyncio
@pytest.mark.parametrize("dependency, satisfied", [
    ("base", True), ("base@local", True), ("base@local@^1", True), ("base@local@^2", False), ("missing", False),
])
async def test_install_receipt_uses_runtime_dependency_identity_and_version(tmp_path, monkeypatch, dependency, satisfied):
    monkeypatch.setattr(config, "STATE_ROOT", tmp_path / "state")
    monkeypatch.setattr(plugin_commands, "STATE_ROOT", tmp_path / "state")
    settings = tmp_path / "settings.json"
    settings.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(plugins, "SETTINGS_FILE", settings)
    policy = ManagedPluginPolicy(enabled_plugins={}, strict_known_marketplaces=None,
        blocked_marketplaces=(), marketplace_requirements={})

    async def hook(**kwargs):
        return None

    async def install(name, dependencies):
        source = tmp_path / name
        (source / ".minicode-plugin").mkdir(parents=True)
        (source / ".minicode-plugin/plugin.json").write_text(json.dumps({
            "name": name, "version": "1.2.0", "dependencies": dependencies,
        }), encoding="utf-8")
        return await plugins.import_plugin_from_path(source, settings_file=settings,
            config_change_hook=hook, _policy=policy)

    await install("base", [])
    result = await install("dependent", [dependency])
    dependent = next(plugin for plugin in result["plugins"] if plugin["name"] == "dependent")
    assert dependent["enabled"] is satisfied
    assert bool(result["imported"].get("missing_dependencies")) is not satisfied
    assert bool([error for error in result["dependency_errors"] if error["required_by"] == "dependent@local"]) is not satisfied
