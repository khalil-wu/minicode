"""An unsubmitted composer reads config without owning a backend thread."""
from __future__ import annotations

import json
from functools import partial
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import config_helpers
from backend.api import routes_llm
from backend.llm.model_runtime import ModelRuntime
from backend.services import draft_model_selection_service as service


@pytest.fixture
def draft_catalog(monkeypatch, tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    settings = state / "settings.json"
    settings.write_text(json.dumps({"llm": {"provider": "custom", "custom": {
        "model": "fixture-model", "available_models": ["fixture-model", "other-model"],
        "wire_api": "chat", "base_url": "https://fixture.invalid/v1", "api_key": "private-credential",
        "headers": {"X-Private": "private-header"},
    }}}), encoding="utf-8")
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    (project / ".minicode").mkdir()
    (project / ".minicode" / "config.toml").write_text('[feature_flags]\nglobal_search = true\n', encoding="utf-8")
    managed = tmp_path / "managed"
    managed.mkdir()
    monkeypatch.setattr(config_helpers, "STATE_ROOT", state)
    monkeypatch.setattr(config_helpers, "SETTINGS_FILE", settings)
    monkeypatch.setenv("MINICODE_SYSTEM_CONFIG_FILE", str(tmp_path / "absent-system.toml"))
    monkeypatch.setenv("MINICODE_MANAGED_SETTINGS_DIR", str(managed))
    monkeypatch.setenv("MINICODE_REQUIREMENTS_FILE", str(tmp_path / "absent-requirements.toml"))
    monkeypatch.delenv("MINICODE_PROFILE", raising=False)
    monkeypatch.setattr("backend.workspace.trust.is_workspace_trusted", lambda _root: True)
    monkeypatch.setattr(service, "ModelRuntime", partial(ModelRuntime, models_path=tmp_path / "absent-models.json"))
    monkeypatch.setattr(service, "get_enabled_composer_command_catalog", lambda root, **_options: [{"name": "fixture", "scope": root}])

    def forbidden(*_args, **_kwargs):
        raise AssertionError("draft metadata cannot consult or mutate the active execution owner")

    app = FastAPI()
    app.include_router(routes_llm.router)
    with TestClient(app) as client:
        with monkeypatch.context() as request_guard:
            request_guard.setattr("backend.workspace.state.get_explicit_active_workspace_root", forbidden)
            request_guard.setattr("backend.workspace.state.set_active_workspace_root", forbidden)
            request_guard.setattr("backend.conversations.repository.ConversationRepository.create_conversation", forbidden)
            request_guard.setattr(config_helpers, "_vault_api_key", forbidden)
            yield SimpleNamespace(client=client, project=project, root=tmp_path)


def disk_snapshot(root):
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def test_scoped_default_and_capabilities_are_read_only_and_do_not_expose_credentials(draft_catalog):
    before = disk_snapshot(draft_catalog.root)
    response = draft_catalog.client.get("/api/llm/draft-selection", params={"workspace_root": str(draft_catalog.project)})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert response.headers["Cache-Control"] == "no-store"
    assert payload["current_model"] == "fixture-model"
    assert payload["models_source"] == "configured"
    assert payload["model_selection_mode"] == "default"
    assert payload["default_model_selection"]["conversation_id"] == ""
    assert payload["default_model_selection"]["working_directory"] == str(draft_catalog.project)
    assert payload["feature_flags"]["global_search"]["enabled"] is True
    assert payload["composer_commands"] == [{"name": "fixture", "scope": str(draft_catalog.project)}]
    assert payload["available_model_options"]
    assert "private-credential" not in response.text and "private-header" not in response.text
    assert disk_snapshot(draft_catalog.root) == before


def test_projectless_read_uses_its_default_base_scope_without_creating_a_task(draft_catalog):
    before = disk_snapshot(draft_catalog.root)
    response = draft_catalog.client.get("/api/llm/draft-selection", params={"workspace_root": "", "config_cwd": str(draft_catalog.project)})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["working_directory"] == ""
    assert payload["config_cwd"] == str(draft_catalog.project)
    assert payload["feature_flags"]["global_search"]["enabled"] is True
    assert disk_snapshot(draft_catalog.root) == before
    assert sorted(path.name for path in draft_catalog.project.iterdir()) == [".git", ".minicode"]


def test_model_edit_validates_the_same_catalog_and_rejects_unavailable_choices_without_writes(draft_catalog):
    before = disk_snapshot(draft_catalog.root)
    accepted = draft_catalog.client.get("/api/llm/draft-selection", params={"workspace_root": str(draft_catalog.project), "model": "other-model"})
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["current_model"] == "other-model"
    assert accepted.json()["default_model_selection"]["model"] == "fixture-model"
    assert accepted.json()["model_selection_mode"] == "model"
    rejected = draft_catalog.client.get("/api/llm/draft-selection", params={"workspace_root": str(draft_catalog.project), "model": "not-registered"})
    assert rejected.status_code == 400 and "unavailable" in rejected.text
    effort = draft_catalog.client.get("/api/llm/draft-selection", params={"workspace_root": str(draft_catalog.project), "reasoning_effort": "not-a-level"})
    assert effort.status_code == 400
    assert disk_snapshot(draft_catalog.root) == before
