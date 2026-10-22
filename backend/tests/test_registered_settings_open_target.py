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
from backend.workspace.api import create_workspace_router


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
    app.include_router(create_workspace_router())
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


@pytest.mark.parametrize("trusted", [True, False])
def test_project_target_resolves_only_the_discovered_scope_with_its_actual_owner(registered_settings, trusted):
    fixture = registered_settings
    path = fixture.workspace / ".minicode" / "config.toml"
    path.parent.mkdir()
    path.write_text('model_verbosity = "high"\n', encoding="utf-8")
    if not trusted:
        fixture.trusted_file.write_text("[]", encoding="utf-8")
    before = fixture.trusted_file.read_bytes()
    payload = fixture.client.get("/api/settings/agent-config", params={"workspace_root": str(fixture.workspace)}).json()
    scope = payload["scopes"][0]
    assert scope["kind"] == "project"
    assert bool(scope["disabled_reason"]) is not trusted
    response = fixture.client.get("/api/settings/open-target", params={"kind": "config", "scope_id": scope["id"],
        "workspace_root": str(fixture.workspace), "path": str(fixture.profile), "project_workspace_root": str(fixture.profile.parent)})
    assert response.status_code == 200, response.text
    assert response.json() == {"path": str(path.resolve()), "workspace_root": str(fixture.workspace)}
    assert fixture.trusted_file.read_bytes() == before
    path.unlink()
    path.parent.rmdir()
    response = fixture.client.get("/api/settings/open-target", params={"kind": "config", "scope_id": scope["id"], "workspace_root": str(fixture.workspace)})
    assert response.status_code == 404, response.text


def test_project_target_cannot_be_replayed_into_a_different_workspace_registry(registered_settings):
    fixture = registered_settings
    folder = fixture.workspace / ".minicode"
    folder.mkdir()
    path = folder / "config.toml"
    path.write_text("# original project\n", encoding="utf-8")
    scope_id = fixture.client.get("/api/settings/agent-config", params={"workspace_root": str(fixture.workspace)}).json()["scopes"][0]["id"]
    other = fixture.workspace.parent / "other-workspace"
    (other / ".git").mkdir(parents=True)
    response = fixture.client.get("/api/settings/open-target", params={"kind": "config", "scope_id": scope_id, "workspace_root": str(other)})
    assert response.status_code == 404, response.text
    assert path.read_text(encoding="utf-8") == "# original project\n"


def editor_session(fixture, monkeypatch, workspace, conversation_id=""):
    from unittest.mock import Mock
    from backend.api import _state
    summary = SimpleNamespace(archived=False, execution_cwd=str(workspace), worktree_path="", workspace_root=str(workspace))
    repository = SimpleNamespace(get_conversation_summary=Mock(return_value=summary))
    session = SimpleNamespace(session_id="settings-editor-session", active_conversation_id=conversation_id,
        conversation_repo=repository, environment_manager=None, editor_config_files={})
    monkeypatch.setattr(_state, "ws_manager", SimpleNamespace(get_session=lambda value: session if value == session.session_id else None))
    return session, {"workspace_root": str(workspace), "session_id": session.session_id, "conversation_id": conversation_id}


@pytest.mark.parametrize("conversation_id", ["", "actual-editor-chat"])
def test_registered_project_tabs_read_compare_write_and_repair_invalid_toml_without_granting_their_directory(registered_settings, monkeypatch, conversation_id):
    fixture = registered_settings
    parent_file = fixture.workspace / ".minicode" / "config.toml"
    parent_file.parent.mkdir()
    parent_file.write_text('model_verbosity = "high" # parent\n', encoding="utf-8")
    child = fixture.workspace / "child"
    child_file = child / ".minicode" / "config.toml"
    child_file.parent.mkdir(parents=True)
    child_file.write_text('web_search = "cached" # child\n', encoding="utf-8")
    neighbor = fixture.workspace / "private-neighbor.txt"
    neighbor.write_text("never authorized by settings open", encoding="utf-8")
    session, owner = editor_session(fixture, monkeypatch, child, conversation_id)
    trusted_before = fixture.trusted_file.read_bytes()
    profile_before = fixture.profile.read_bytes()
    user_before = fixture.settings.read_bytes()
    for path in (parent_file, child_file):
        opened = fixture.client.get("/api/settings/open-target", params={**owner, "kind": "config", "scope_id": f"project:{path.parent.parent}"})
        assert opened.status_code == 200, opened.text
        assert opened.json()["path"] == str(path)
    assert len(session.editor_config_files) == 2
    parent_read = fixture.client.get("/api/workspace/file", params={**owner, "path": str(parent_file)})
    child_read = fixture.client.get("/api/workspace/file", params={**owner, "path": str(child_file)})
    assert parent_read.status_code == child_read.status_code == 200, (parent_read.text, child_read.text)
    assert parent_read.json()["workspace_root"] == str(child)
    invalid = 'model_verbosity = "unfinished'
    written = fixture.client.put("/api/workspace/file/compare-write", params=owner,
        json={"path": str(parent_file), "expected_hash": parent_read.json()["content_hash"], "content": invalid})
    assert written.status_code == 200, written.text
    assert parent_file.read_text(encoding="utf-8") == invalid
    reread = fixture.client.get("/api/workspace/file", params={**owner, "path": str(parent_file)})
    assert reread.status_code == 200 and reread.json()["content"] == invalid, reread.text
    stale = fixture.client.put("/api/workspace/file/compare-write", params=owner,
        json={"path": str(parent_file), "expected_hash": parent_read.json()["content_hash"], "content": "must not overwrite"})
    assert stale.status_code == 409, stale.text
    fixed = 'model_verbosity = "medium" # repaired\n'
    repaired = fixture.client.put("/api/workspace/file/compare-write", params=owner,
        json={"path": str(parent_file), "expected_hash": written.json()["content_hash"], "content": fixed})
    assert repaired.status_code == 200, repaired.text
    child_written = fixture.client.put("/api/workspace/file/compare-write", params=owner,
        json={"path": str(child_file), "expected_hash": child_read.json()["content_hash"], "content": 'web_search = "live" # child retained\n'})
    assert child_written.status_code == 200, child_written.text
    assert parent_file.read_text(encoding="utf-8") == fixed
    denied_status = 400 if conversation_id else 403
    assert fixture.client.get("/api/workspace/file", params={**owner, "path": str(neighbor)}).status_code == denied_status
    assert fixture.client.put("/api/workspace/file/compare-write", params=owner,
        json={"path": str(neighbor), "expected_hash": "", "content": "must not write"}).status_code == denied_status
    assert neighbor.read_text(encoding="utf-8") == "never authorized by settings open"
    assert fixture.trusted_file.read_bytes() == trusted_before
    assert fixture.profile.read_bytes() == profile_before and fixture.settings.read_bytes() == user_before
    if not conversation_id:
        session.conversation_repo.get_conversation_summary.assert_not_called()


def test_project_editor_registration_and_reads_keep_the_actual_session_and_conversation_owner(registered_settings, monkeypatch):
    fixture = registered_settings
    folder = fixture.workspace / ".minicode"
    folder.mkdir()
    path = folder / "config.toml"
    path.write_text("# original\n", encoding="utf-8")
    session, owner = editor_session(fixture, monkeypatch, fixture.workspace, "editor-chat")
    opened = fixture.client.get("/api/settings/open-target", params={**owner, "kind": "config", "scope_id": f"project:{fixture.workspace}"})
    assert opened.status_code == 200, opened.text
    session.active_conversation_id = "next-chat"
    denied = fixture.client.get("/api/workspace/file", params={**owner, "path": str(path)})
    assert denied.status_code == 409 and "owner is stale" in denied.text
    other_session = {**owner, "session_id": "another-window-session"}
    denied = fixture.client.get("/api/workspace/file", params={**other_session, "path": str(path)})
    assert denied.status_code == 404, denied.text
    denied = fixture.client.get("/api/settings/open-target", params={**owner, "kind": "config", "scope_id": f"project:{fixture.workspace}"})
    assert denied.status_code == 409, denied.text
    assert path.read_text(encoding="utf-8") == "# original\n"
