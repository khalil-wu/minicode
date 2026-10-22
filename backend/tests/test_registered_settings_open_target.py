"""Settings actions resolve registered sources without granting workspace access."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import config
from backend.api import routes_llm
from backend.config_layers import load_config_layers_state
from backend.services import plugin_settings_service
from backend.services import agent_config_settings_service
from backend.workspace import trust


@pytest.fixture
def registered_settings(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    (workspace / ".git").mkdir(parents=True)
    profile_home = tmp_path / "profile"
    profile_home.mkdir()
    settings = profile_home / "settings.json"
    settings.write_text("{}", encoding="utf-8")
    profile = profile_home / "audit.config.toml"
    profile.write_text(
        '[[hooks.pre_tool_use]]\n[[hooks.pre_tool_use.hooks]]\n'
        'type = "command"\ncommand = "node profile-hook.js"\n',
        encoding="utf-8",
    )
    data = profile_home / "data"
    data.mkdir()
    plugin = tmp_path / "installed" / "fixture-plugin"
    manifest = plugin / ".minicode-plugin" / "plugin.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"name": "fixture-plugin", "hooks": {
        "stop": [{"hooks": [{"type": "command", "command": "node installed-hook.js"}]}],
    }}), encoding="utf-8")
    trusted_file = tmp_path / "trusted-workspaces.json"
    trusted_file.write_text(json.dumps([str(workspace)]), encoding="utf-8")
    monkeypatch.setattr(trust, "TRUSTED_WORKSPACES_FILE", trusted_file)
    monkeypatch.setattr(routes_llm, "SETTINGS_FILE", settings)
    monkeypatch.setattr(config, "DATA_ROOT", data)

    def load_stack(*, cwd=None):
        return load_config_layers_state(
            state_root=profile_home, user_config_file=settings, profile="audit", cwd=cwd,
            system_config_path=tmp_path / "absent-system.toml",
            requirements_path=tmp_path / "absent-requirements.toml",
            trust_resolver=trust.is_workspace_trusted,
        )

    monkeypatch.setattr(config, "load_config_layer_stack", load_stack)
    monkeypatch.setattr(agent_config_settings_service, "load_config_layer_stack", load_stack)
    monkeypatch.setattr(plugin_settings_service, "get_plugin_snapshot", lambda **_kwargs: {
        "plugins_enabled": True,
        "plugins": [{"id": "fixture-plugin", "path": str(plugin), "enabled": True,
                     "manifest_paths": [str(manifest)]}],
    })
    monkeypatch.setattr(plugin_settings_service, "plugin_install_root", lambda: plugin.parent)
    app = FastAPI()
    app.include_router(routes_llm.router)
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, workspace=workspace, settings=settings,
                              profile=profile, manifest=manifest, data=data,
                              trusted_file=trusted_file)


@pytest.mark.parametrize("kind", ["config", "data"])
def test_global_target_ignores_forged_path_and_runtime_outside_workspace(registered_settings, kind):
    fixture = registered_settings
    target = fixture.settings if kind == "config" else fixture.data
    before = fixture.trusted_file.read_bytes()
    response = fixture.client.get("/api/settings/open-target", params={
        "kind": kind, "workspace_root": str(fixture.workspace),
        "path": str(fixture.workspace / "forged.json"),
        "apiBaseUrl": "http://untrusted.invalid", "runtimeToken": "renderer-token",
    })
    assert response.status_code == 200, response.text
    assert response.json() == {"path": str(target.resolve())}
    assert response.headers["cache-control"] == "no-store"
    assert not trust.is_workspace_trusted(target.parent)
    assert fixture.trusted_file.read_bytes() == before


def test_user_scope_opens_the_actual_active_profile_and_retains_the_legacy_global_action(registered_settings):
    fixture = registered_settings
    response = fixture.client.get("/api/settings/open-target", params={"kind": "config", "scope_id": "user", "workspace_root": str(fixture.workspace)})
    assert response.status_code == 200, response.text
    assert response.json() == {"path": str(fixture.profile.resolve())}
    assert fixture.client.get("/api/settings/open-target", params={"kind": "config"}).json() == {"path": str(fixture.settings.resolve())}
    assert fixture.client.get("/api/settings/open-target", params={"kind": "config", "scope_id": "managed"}).status_code == 404


@pytest.mark.parametrize("source", ["profile", "manifest"])
def test_hook_action_resolves_the_current_profile_or_installed_registry_source(registered_settings, source):
    fixture = registered_settings
    target: Path = getattr(fixture, source)
    inventory = fixture.client.get("/api/settings/hooks", params={"workspace_root": str(fixture.workspace)})
    assert inventory.status_code == 200, inventory.text
    hook = next(entry for entry in inventory.json()["hooks"] if entry["source_path"] == str(target.resolve()))
    before = fixture.trusted_file.read_bytes()
    response = fixture.client.get("/api/settings/open-target", params={
        "kind": "hook", "workspace_root": str(fixture.workspace), "hook_key": hook["key"],
        "path": str(fixture.workspace / "renderer-selected.json"),
    })
    assert response.status_code == 200, response.text
    assert response.json() == {"path": str(target.resolve())}
    assert response.headers["cache-control"] == "no-store"
    assert not trust.is_workspace_trusted(target.parent)
    assert fixture.trusted_file.read_bytes() == before


@pytest.mark.parametrize("source", ["profile", "manifest"])
def test_removed_source_invalidates_the_previous_hook_key(registered_settings, source):
    fixture = registered_settings
    target: Path = getattr(fixture, source)
    params = {"workspace_root": str(fixture.workspace)}
    inventory = fixture.client.get("/api/settings/hooks", params=params)
    hook = next(entry for entry in inventory.json()["hooks"] if entry["source_path"] == str(target.resolve()))
    target.unlink()
    response = fixture.client.get("/api/settings/open-target", params={**params, "kind": "hook", "hook_key": hook["key"]})
    assert response.status_code == 404
    assert response.json() == {"detail": "Hook is no longer registered; reload its configuration."}
    assert all(entry["key"] != hook["key"] for entry in fixture.client.get("/api/settings/hooks", params=params).json()["hooks"])


def test_an_arbitrary_path_is_not_a_registered_action(registered_settings):
    fixture = registered_settings
    response = fixture.client.get("/api/settings/open-target", params={"kind": "path", "path": str(fixture.profile)})
    assert response.status_code == 422
    response = fixture.client.get("/api/settings/open-target", params={"kind": "hook", "hook_key": str(fixture.manifest), "workspace_root": str(fixture.workspace)})
    assert response.status_code == 404
