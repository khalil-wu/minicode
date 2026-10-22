"""Real files and HTTP receipts for User/profile/Admin configuration ownership."""
from __future__ import annotations

import json
import tomllib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import config_helpers
from backend.api import routes_llm
from backend.services import agent_config_settings_service as service


@pytest.fixture
def catalog(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    user = home / "settings.json"
    user.write_text(json.dumps({"model_verbosity": "low", "llm": {"custom": {"api_key": "private-user-key"}}}), encoding="utf-8")
    workspace = tmp_path / "workspace"
    (workspace / ".git").mkdir(parents=True)
    (workspace / ".minicode").mkdir()
    managed = tmp_path / "managed"
    managed.mkdir()
    requirements = tmp_path / "requirements.toml"
    monkeypatch.setattr(config_helpers, "STATE_ROOT", home)
    monkeypatch.setattr(config_helpers, "SETTINGS_FILE", user)
    monkeypatch.setenv("MINICODE_SYSTEM_CONFIG_FILE", str(tmp_path / "absent-system.toml"))
    monkeypatch.setenv("MINICODE_MANAGED_SETTINGS_DIR", str(managed))
    monkeypatch.setenv("MINICODE_REQUIREMENTS_FILE", str(requirements))
    monkeypatch.delenv("MINICODE_PROFILE", raising=False)
    monkeypatch.setattr("backend.workspace.trust.is_workspace_trusted", lambda _root: True)
    hook = AsyncMock(return_value=None)
    monkeypatch.setattr(service, "run_config_change_hook", hook)
    app = FastAPI()
    app.include_router(routes_llm.router)
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, home=home, user=user, workspace=workspace, managed=managed,
                              requirements=requirements, hook=hook, monkeypatch=monkeypatch)


def read(catalog):
    response = catalog.client.get("/api/settings/agent-config", params={"workspace_root": str(catalog.workspace)})
    assert response.status_code == 200, response.text
    return response.json()


def save(catalog, payload, **patch):
    user = next(scope for scope in payload["scopes"] if scope["id"] == "user")
    return catalog.client.put("/api/settings/agent-config", json={"workspace_root": str(catalog.workspace),
        "scope_id": "user", "expected_version": user["version"], **patch})


def test_update_transport_preserves_scope_version_and_explicit_null():
    request = routes_llm.AgentConfigUpdate(scope_id="managed", expected_version="read-version", model_verbosity=None)
    assert request.model_dump(exclude_unset=True) == {"scope_id": "managed", "expected_version": "read-version", "model_verbosity": None}


def test_public_projection_excludes_secrets_and_null_removes_only_the_authored_override(catalog):
    before = read(catalog)
    assert [scope["id"] for scope in before["scopes"]] == ["user"]
    assert before["scopes"][0]["path"] == str(catalog.user)
    assert before["scopes"][0]["values"] == {"model_verbosity": "low"}
    assert "private-user-key" not in json.dumps(before)
    response = save(catalog, before, model_verbosity=None)
    assert response.status_code == 200, response.text
    after = response.json()
    assert after["effective"]["model_verbosity"] is None
    assert "model_verbosity" not in after["scopes"][0]["values"]
    assert after["write"]["status"] == "applied"
    assert after["write"]["fields"]["model_verbosity"]["requested_value"] is None
    assert json.loads(catalog.user.read_text())["llm"]["custom"]["api_key"] == "private-user-key"
    catalog.hook.assert_awaited_once_with(source="user_settings", file_path=str(catalog.user))


def test_last_user_profile_is_read_written_and_opened_without_reformatting_other_values(catalog):
    profile = catalog.home / "audit.config.toml"
    profile.write_text('# authored profile\nmodel_verbosity = "high" # keep comment\n\n[sandbox_workspace_write]\nnetwork_access = false # network comment\nexclude_tmpdir_env_var = true\n', encoding="utf-8")
    catalog.monkeypatch.setenv("MINICODE_PROFILE", "audit")
    before = read(catalog)
    assert before["path"] == before["scopes"][0]["path"] == str(profile)
    assert before["scopes"][0]["source"]["profile"] == "audit"
    response = save(catalog, before, network_access=True)
    assert response.status_code == 200, response.text
    assert response.json()["write"]["path"] == str(profile)
    assert "# authored profile" in profile.read_text()
    assert "# network comment" in profile.read_text()
    assert tomllib.loads(profile.read_text())["sandbox_workspace_write"] == {"network_access": True, "exclude_tmpdir_env_var": True}
    response = save(catalog, response.json(), model_verbosity=None)
    assert response.status_code == 200, response.text
    assert "model_verbosity" not in tomllib.loads(profile.read_text())
    assert response.json()["effective"]["model_verbosity"] == "low"
    assert json.loads(catalog.user.read_text())["model_verbosity"] == "low"
    opened = catalog.client.get("/api/settings/open-target", params={"kind": "config", "scope_id": "user", "workspace_root": str(catalog.workspace)})
    assert opened.status_code == 200, opened.text
    assert opened.json() == {"path": str(profile)}


def test_stale_version_rejects_before_writing_and_a_fresh_patch_preserves_the_other_edit(catalog):
    before = read(catalog)
    external = json.loads(catalog.user.read_text())
    external["model_reasoning_summary"] = "detailed"
    catalog.user.write_text(json.dumps(external), encoding="utf-8")
    bytes_before = catalog.user.read_bytes()
    response = save(catalog, before, model_verbosity="high")
    assert response.status_code == 409, response.text
    assert "changed since it was read" in response.json()["detail"]
    assert catalog.user.read_bytes() == bytes_before
    response = save(catalog, read(catalog), model_verbosity="high")
    assert response.status_code == 200, response.text
    assert json.loads(catalog.user.read_text())["model_reasoning_summary"] == "detailed"


def test_written_user_value_reports_actual_project_override_without_false_failure(catalog):
    (catalog.workspace / ".minicode" / "config.toml").write_text('web_search = "live"\n', encoding="utf-8")
    before = read(catalog)
    response = save(catalog, before, web_search="disabled")
    assert response.status_code == 200, response.text
    after = response.json()
    assert after["write"]["status"] == "overridden"
    assert after["scopes"][0]["values"]["web_search"] == "disabled"
    assert after["effective"]["web_search"] == "live"
    assert after["write"]["fields"]["web_search"]["overriding_source"]["source"]["type"] == "project"
    assert json.loads(catalog.user.read_text())["web_search"] == "disabled"


def test_real_managed_scope_is_readonly_and_locks_only_its_effective_fields(catalog):
    managed_file = catalog.managed / "managed-settings.json"
    managed_file.write_text(json.dumps({"sandbox_mode": "read-only", "llm": {"custom": {"api_key": "private-admin-key"}}}), encoding="utf-8")
    before = read(catalog)
    admin = before["scopes"][1]
    assert admin["id"] == "managed" and admin["readonly"] is True
    assert admin["values"] == {"sandbox_mode": "read-only"}
    assert before["origins"]["sandbox_mode"]["source"]["type"] == "policy"
    assert "private-admin-key" not in json.dumps(before)
    original = catalog.user.read_bytes()
    assert save(catalog, before, sandbox_mode="workspace-write").status_code == 409
    assert catalog.client.put("/api/settings/agent-config", json={"scope_id": "managed", "model_verbosity": "high"}).status_code == 409
    assert catalog.user.read_bytes() == original
    catalog.hook.assert_not_awaited()
    assert save(catalog, before, model_verbosity="high").status_code == 200
    opened = catalog.client.get("/api/settings/open-target", params={"kind": "config", "scope_id": "managed"})
    assert opened.json() == {"path": str(managed_file)}


def test_requirements_are_public_options_and_cannot_be_bypassed_by_unset(catalog):
    catalog.requirements.write_text('allowed_sandbox_modes = ["read-only"]\n', encoding="utf-8")
    before = read(catalog)
    assert before["requirements"]["allowed_sandbox_modes"] == ["read-only"]
    original = catalog.user.read_bytes()
    assert save(catalog, before, sandbox_mode="danger-full-access").status_code == 409
    assert save(catalog, before, sandbox_mode=None).status_code == 409
    assert catalog.user.read_bytes() == original


def test_atomic_write_failure_is_an_error_and_keeps_the_original_user_file(catalog):
    before = read(catalog)
    original = catalog.user.read_bytes()
    def denied(*_args, **_kwargs):
        raise OSError("fixture disk write rejected")
    catalog.monkeypatch.setattr(config_helpers, "atomic_write_text", denied)
    response = save(catalog, before, model_verbosity="high")
    assert response.status_code == 500
    assert response.json() == {"detail": "fixture disk write rejected"}
    assert catalog.user.read_bytes() == original


def test_merged_managed_sources_have_real_values_but_no_fake_single_open_path(catalog):
    (catalog.managed / "managed-settings.json").write_text('{"model_verbosity":"medium"}', encoding="utf-8")
    dropins = catalog.managed / "managed-settings.d"
    dropins.mkdir()
    (dropins / "10-sandbox.json").write_text('{"sandbox_mode":"read-only"}', encoding="utf-8")
    admin = read(catalog)["scopes"][1]
    assert admin["values"] == {"model_verbosity": "medium", "sandbox_mode": "read-only"}
    assert admin["path"] is None
    response = catalog.client.get("/api/settings/open-target", params={"kind": "config", "scope_id": "managed"})
    assert response.status_code == 404
    assert "no individual file" in response.json()["detail"]


def test_new_profile_write_creates_only_that_registered_user_file(catalog):
    catalog.monkeypatch.setenv("MINICODE_PROFILE", "new-profile")
    before = read(catalog)
    original = catalog.user.read_bytes()
    response = save(catalog, before, network_access=True)
    assert response.status_code == 200, response.text
    profile = catalog.home / "new-profile.config.toml"
    assert response.json()["write"]["path"] == str(profile)
    assert tomllib.loads(profile.read_text())["sandbox_workspace_write"]["network_access"] is True
    assert catalog.user.read_bytes() == original
