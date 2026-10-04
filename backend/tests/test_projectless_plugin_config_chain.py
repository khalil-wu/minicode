from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import config, config_helpers, config_layers
from backend.api import routes_skills
from backend.extensions.capability_source import ExtensionCapabilitySource
from backend.services import plugin_settings_service as plugins
from backend.skills.loader import SkillLoader


@pytest.fixture
def scopes(tmp_path, monkeypatch):
    active = tmp_path / "active"
    foreign = tmp_path / "foreign"
    for root, enabled, iterations in ((active, False, 23), (foreign, True, 31)):
        (root / ".git").mkdir(parents=True)
        (root / ".minicode").mkdir()
        (root / ".minicode/config.toml").write_text(
            f'[plugins."docs@local"]\nenabled = {str(enabled).lower()}\n[agent]\nmax_iterations = {iterations}\n', encoding="utf-8"
        )
    user = tmp_path / "settings.json"
    user.write_text(json.dumps({"plugins": {"docs@local": {"enabled": True}}, "agent": {"max_iterations": 7}}), encoding="utf-8")
    install = tmp_path / "installed"
    manifest = install / "docs/.minicode-plugin/plugin.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"name": "docs", "version": "1.0.0", "skills": "skills"}), encoding="utf-8")
    (install / "docs/skills").mkdir()
    monkeypatch.setattr(config_helpers, "STATE_ROOT", tmp_path / "state")
    monkeypatch.setattr(config_helpers, "SETTINGS_FILE", user)
    monkeypatch.setattr(config_layers, "default_system_config_path", lambda: tmp_path / "missing-system.toml")
    monkeypatch.setattr("backend.managed_settings.load_minicode_managed_settings", lambda *args, **kwargs: SimpleNamespace(configured=False, validation_errors=()))
    monkeypatch.setenv("MINICODE_REQUIREMENTS_FILE", str(tmp_path / "missing-requirements.toml"))
    monkeypatch.delenv("MINICODE_PROFILE", raising=False)
    monkeypatch.setattr("backend.workspace.state.get_explicit_active_workspace_root", lambda: active)
    monkeypatch.setattr("backend.workspace.trust.is_workspace_trusted", lambda root: True)
    monkeypatch.setattr("backend.commands.plugins.default_plugin_roots", lambda: [install])
    monkeypatch.setenv("MINICODE_PLUGINS_DIR", str(install))
    monkeypatch.setattr(plugins, "_load_settings_json", lambda: json.loads(user.read_text(encoding="utf-8")))
    monkeypatch.setattr(config, "load_llm_settings", lambda data: config_helpers.LLMSettings(api_key=""))
    return active, foreign


def test_explicit_projectless_config_does_not_inherit_active_project_and_omission_keeps_legacy_scope(scopes):
    active, foreign = scopes
    assert config_helpers.load_config_layer_stack().effective_config()["agent"]["max_iterations"] == 23
    assert config_helpers.load_config_layer_stack(cwd=foreign).effective_config()["agent"]["max_iterations"] == 31
    explicit = config_helpers.load_config_layer_stack(cwd=None)
    assert explicit.effective_config()["agent"]["max_iterations"] == 7
    assert not any(layer.source.kind == "project" for layer in explicit.layers)
    assert config_helpers._load_effective_settings_json(cwd=None)["agent"]["max_iterations"] == 7
    assert config_helpers._load_effective_settings_json()["agent"]["max_iterations"] == 23


def test_app_config_projects_the_same_explicit_owner_for_actual_turn_consumers(scopes):
    _, foreign = scopes
    assert config.load_config().agent.max_iterations == 23
    assert config.load_config(cwd=None).agent.max_iterations == 7
    assert config.load_config(cwd=foreign).agent.max_iterations == 31


@pytest.mark.parametrize("scope,expected", [(None, False), ("", True), ("foreign", True), ("active", False)])
def test_actual_plugin_catalog_api_uses_requested_owner_config_once(scopes, scope, expected):
    active, foreign = scopes
    app = FastAPI()
    app.include_router(routes_skills.router)
    requested = foreign if scope == "foreign" else active if scope == "active" else scope
    params = {} if requested is None else {"workspace_root": str(requested)}
    with TestClient(app) as client:
        response = client.get("/api/plugins", params=params)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["plugins"][0]["enabled"] is expected


def test_plugin_catalog_rejects_missing_requested_directory_without_borrowing_active(scopes, tmp_path):
    app = FastAPI()
    app.include_router(routes_skills.router)
    with TestClient(app) as client:
        response = client.get("/api/plugins", params={"workspace_root": str(tmp_path / "missing")})
    assert response.status_code == 409


def test_projectless_extension_fingerprint_and_skill_plugin_catalog_share_user_enablement(scopes):
    source = ExtensionCapabilitySource(session_owner="session", owner_id="projectless", workspace_root=None,
        project_trusted=False, on_model_change=lambda *args: None)
    source.fingerprint()
    assert source._plugin_snapshot["plugins"][0]["enabled"] is True
    assert not any(layer.source.kind == "project" for layer in source._config_stack.layers)
    # The active project disables docs; this owned loader must retain its user-selected skills.
    assert SkillLoader(None)._plugin_search_dirs()


def test_plugin_mention_resolution_uses_explicit_projectless_owner(scopes):
    assert plugins.resolve_enabled_plugin_mentions([{"path": "plugin://docs"}], workspace_root=None)[0]["config_name"] == "docs@local"
