from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import Response

from backend.api import routes_skills
from backend.config_layers import ConfigLayer, ConfigLayerSource, ConfigLayerStack
from backend.config_requirements import ConfigRequirements
from backend.plugins.manager import PluginManager
from backend.plugins.source_agent_manifest import AGENT_PLUGIN_SCHEMA
from backend.services import plugin_settings_service, skills_api_service
from backend.skills.loader import SkillLoader
from backend.skills.manager import SkillManager


@pytest.fixture
def plugin_catalog(tmp_path, monkeypatch):
    from backend.commands import plugins as commands

    installed = tmp_path / "installed"
    root = installed / "docs"
    manifest = root / ".minicode-plugin/plugin.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"name": "docs", "version": "1.2.3", "skills": ["./skills", "./extra"],
        "interface": {"developerName": "Local author", "category": "Productivity", "capabilities": ["Read", "Write"],
            "websiteUrl": "https://docs.example/", "privacyPolicyUrl": "https://docs.example/privacy", "termsOfServiceUrl": "https://docs.example/terms"}}), encoding="utf8")
    settings = {"plugins": {"docs@local": {"enabled": True}}, "feature_flags": {"plugins": True}}
    settings_file = tmp_path / "settings.json"
    def stack(**_kwargs):
        return ConfigLayerStack((ConfigLayer(ConfigLayerSource("user", file=str(settings_file)), deepcopy(settings)),))
    monkeypatch.setattr("backend.config.load_config_layer_stack", stack)
    monkeypatch.setattr(plugin_settings_service, "_load_settings_json", lambda: deepcopy(settings))
    monkeypatch.setattr(commands, "default_plugin_roots", lambda: [installed])
    monkeypatch.setenv("MINICODE_PLUGINS_DIR", str(installed))
    monkeypatch.setattr(SkillLoader, "_search_dirs", lambda self: self._plugin_search_dirs())
    monkeypatch.setattr(skills_api_service, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(skills_api_service, "_update_settings_json", lambda change: change(settings))
    async def hook(**_kwargs):
        return None
    monkeypatch.setattr(skills_api_service, "run_config_change_hook", hook)
    manager = SkillManager(SkillLoader())
    monkeypatch.setattr(routes_skills._state, "bootstrap", SimpleNamespace(skill_manager=manager))
    monkeypatch.setattr(routes_skills._state, "invalidate_status_cache", lambda: None)
    def skill(relative, name="review", *, products=None):
        path = root / relative / "SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_text(f"---\nname: {name}\ndescription: Actual workflow\n---\n# Workflow\n\nOriginal instructions.\n", encoding="utf8", newline="")
        if products:
            metadata = path.parent / "agents/openai.yaml"
            metadata.parent.mkdir()
            metadata.write_text("policy:\n  products: [chatgpt]\n", encoding="utf8")
        return path
    return SimpleNamespace(root=root, manifest=manifest, settings=settings, manager=manager, stack=stack, skill=skill)


@pytest.mark.asyncio
async def test_disabled_plugin_keeps_exact_readable_settings_inventory_without_admitting_execution(plugin_catalog):
    fixture = plugin_catalog
    path = fixture.skill("skills/nested/review")
    admitted = fixture.manager.snapshot()
    fixture.settings["plugins"]["docs@local"]["enabled"] = False
    payload = await routes_skills.get_skill_settings_api(Response())
    assert payload["available_skills"] == []
    assert len(payload["plugin_skills"]) == 1
    assert payload["plugin_skills"][0]["enabled"] is True
    assert payload["plugin_skills"][0]["plugin_enabled"] is False
    snapshot = fixture.manager.snapshot()
    assert snapshot.readable_roots() == []
    assert snapshot.load_skill_payload("docs@local:review", path) is None
    detail = await routes_skills.get_skill_detail_api(Response(), skill_path=str(path))
    assert detail["raw_content"] == path.read_bytes().decode("utf8")
    assert detail["plugin_enabled"] is False
    await routes_skills.update_skill_settings_api(routes_skills.SkillEnabledRequest(path=str(path), enabled=False), Response())
    changed = await routes_skills.update_skill_settings_api(routes_skills.SkillEnabledRequest(path=str(path), enabled=True), Response())
    assert changed["plugin_skills"][0]["enabled"] is True
    assert changed["available_skills"] == []
    assert admitted.load_skill_payload("docs@local:review", path) is not None
    fixture.settings["plugins"]["docs@local"]["enabled"] = True
    assert fixture.manager.snapshot().load_skill_payload("docs@local:review", path) is not None
    assert snapshot.readable_roots() == []


@pytest.mark.parametrize("agent_plugin,expected", [(False, 4), (True, 1)])
def test_count_and_runtime_share_format_validation_and_exact_path_inventory(plugin_catalog, agent_plugin, expected):
    fixture = plugin_catalog
    fixture.skill("skills/direct", "direct")
    fixture.skill("skills/first/review")
    fixture.skill("skills/second/review")
    fixture.skill("extra/review")
    fixture.skill("skills/chatgpt-only", "chatgpt-only", products=True)
    malformed = fixture.root / "skills/broken/SKILL.md"
    malformed.parent.mkdir()
    malformed.write_text("No Skill frontmatter", encoding="utf8")
    if agent_plugin:
        (fixture.root / "plugin.json").write_text(json.dumps({"$schema": AGENT_PLUGIN_SCHEMA, "name": "docs", "version": "1.2.3"}), encoding="utf8")
    plugin = PluginManager(config_stack=fixture.stack()).snapshot().plugins[0]
    inventory = fixture.manager.snapshot().list_settings(source_level="plugin")
    if agent_plugin:
        assert plugin["manifest_paths"] == [str(fixture.root / "plugin.json")]
    assert plugin["skill_count"] == expected == len(inventory)
    assert len({skill["path"] for skill in inventory}) == expected
    assert not any("broken" in skill["path"] or "chatgpt-only" in skill["path"] for skill in inventory)


def test_local_information_urls_and_fields_survive_plugin_payload(plugin_catalog):
    plugin = PluginManager(config_stack=plugin_catalog.stack()).snapshot().plugins[0]
    assert plugin["developerName"] == "Local author"
    assert plugin["category"] == "Productivity"
    assert plugin["capabilities"] == ["Read", "Write"]
    assert plugin["websiteUrl"] == "https://docs.example/"
    assert plugin["privacyPolicyUrl"] == "https://docs.example/privacy"
    assert plugin["termsOfServiceUrl"] == "https://docs.example/terms"


@pytest.mark.parametrize("configured,required,expected", [
    (True, None, True), (False, None, False), (True, False, False), (False, True, True),
])
def test_captured_skill_and_plugin_flags_do_not_reload_global_config(plugin_catalog, monkeypatch, configured, required, expected):
    from backend import config

    fixture = plugin_catalog
    fixture.skill("skills/review")
    values = deepcopy(fixture.settings)
    values["features"] = {"plugin_skills": configured, "plugin_lifecycle_api": configured}
    requirements = {"strict_plugin_only_customization": ["skills"]}
    if required is not None:
        requirements["features"] = {"plugin_skills": required, "plugin_lifecycle_api": required}
    stack = ConfigLayerStack(
        (ConfigLayer(ConfigLayerSource("session_flags"), values),),
        requirements=ConfigRequirements(raw=requirements),
    )
    fixture.manager._loader._config_stack = stack
    for name in ("PLUGIN_SKILLS", "PLUGIN_LIFECYCLE_API", "PLUGINS"):
        monkeypatch.delenv(f"MINICODE_FEATURE_{name}", raising=False)
    reads = []

    def unexpected_global_read(*_args, **_kwargs):
        reads.append(True)
        raise AssertionError("A captured skill snapshot must use its captured configuration")

    monkeypatch.setattr(config, "load_config_layer_stack", unexpected_global_read)
    monkeypatch.setattr(config, "_load_effective_settings_json", unexpected_global_read)
    monkeypatch.setattr(config, "get_config_requirements", unexpected_global_read)
    assert fixture.manager._loader._plugin_only_customization() is True
    snapshot = fixture.manager.snapshot(config_stack=stack)
    assert bool(snapshot.list_metas()) is expected
    assert PluginManager(config_stack=stack).snapshot().get("feature_enabled") is expected
    assert reads == []


def test_next_turn_skill_snapshot_sees_edits_removals_installs_and_new_config(plugin_catalog):
    fixture = plugin_catalog
    path = fixture.skill("skills/review")
    captured = fixture.manager.snapshot(config_stack=fixture.stack())
    path.write_text(path.read_text(encoding="utf8").replace("Actual workflow", "Updated workflow"), encoding="utf8")
    updated = fixture.manager.snapshot(config_stack=fixture.stack())
    assert captured.list_metas()[0].description == "Actual workflow"
    assert updated.list_metas()[0].description == "Updated workflow"
    path.unlink()
    assert fixture.manager.snapshot(config_stack=fixture.stack()).list_metas() == []
    fixture.skill("skills/new", "installed")
    installed = fixture.manager.snapshot(config_stack=fixture.stack())
    assert [meta.name for meta in installed.list_metas()] == ["docs@local:installed"]
    fixture.settings["features"] = {"plugin_skills": False}
    assert fixture.manager.snapshot(config_stack=fixture.stack()).list_metas() == []
    assert [meta.name for meta in installed.list_metas()] == ["docs@local:installed"]
