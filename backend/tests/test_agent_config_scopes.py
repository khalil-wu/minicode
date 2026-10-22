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


def read(catalog, workspace=None):
    response = catalog.client.get("/api/settings/agent-config", params={"workspace_root": str(workspace or catalog.workspace)})
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
    (catalog.workspace / ".minicode").mkdir()
    (catalog.workspace / ".minicode" / "config.toml").write_text('web_search = "live"\n', encoding="utf-8")
    before = read(catalog)
    response = save(catalog, before, web_search="disabled")
    assert response.status_code == 200, response.text
    after = response.json()
    assert after["write"]["status"] == "overridden"
    assert next(scope for scope in after["scopes"] if scope["id"] == "user")["values"]["web_search"] == "disabled"
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


def project_file(workspace, text=""):
    path = workspace / ".minicode" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def save_project(catalog, payload, workspace, owner, **patch):
    scope = next(scope for scope in payload["scopes"] if scope["workspace_root"] == str(owner))
    return catalog.client.put("/api/settings/agent-config", json={"workspace_root": str(workspace),
        "scope_id": scope["id"], "expected_version": scope["version"], **patch})


def test_project_catalog_preserves_actual_layer_order_owners_raw_fields_and_disabled_reason(catalog):
    root_file = project_file(catalog.workspace, 'web_search = "live"\n[llm.custom]\napi_key = "ignored-project-secret"\n')
    child = catalog.workspace / "app"
    child_file = project_file(child, 'model_verbosity = "high"\n')
    catalog.monkeypatch.setattr("backend.workspace.trust.is_workspace_trusted", lambda root: root != catalog.workspace)
    payload = read(catalog, child)
    scopes = payload["scopes"]
    assert [scope["kind"] for scope in scopes] == ["project", "project", "user"]
    assert [scope["workspace_root"] for scope in scopes] == [str(catalog.workspace), str(child), None]
    assert [scope["path"] for scope in scopes] == [str(root_file), str(child_file), str(catalog.user)]
    assert payload["default_scope_id"] == scopes[1]["id"] == f"project:{child}"
    assert scopes[0]["disabled_reason"] and "not trusted" in scopes[0]["disabled_reason"]
    assert scopes[0]["values"] == {"web_search": "live"}
    assert scopes[0]["readonly"] is False
    assert scopes[1]["disabled_reason"] is None
    assert scopes[1]["values"] == {"model_verbosity": "high"}
    assert payload["effective"]["web_search"] == "cached"
    assert payload["effective"]["model_verbosity"] == "high"
    assert scopes[2]["disabled_reason"] is None
    assert "ignored-project-secret" not in json.dumps(payload)
    assert "private-user-key" not in json.dumps(payload)


def test_scope_default_uses_only_discovered_projects_and_keeps_projectless_user(catalog):
    assert read(catalog)["default_scope_id"] == "user"
    project_file(catalog.workspace)
    child = catalog.workspace / "app"
    child.mkdir()
    payload = read(catalog, child)
    assert payload["default_scope_id"] == f"project:{catalog.workspace}"
    assert [scope["kind"] for scope in payload["scopes"]] == ["project", "user"]
    response = catalog.client.get("/api/settings/agent-config")
    assert response.status_code == 200, response.text
    assert [scope["id"] for scope in response.json()["scopes"]] == ["user"]


@pytest.mark.parametrize("sandbox_table", [
    '[sandbox_workspace_write]\nnetwork_access = false # keep network\nexclude_tmpdir_env_var = true\n\n',
    'sandbox_workspace_write = { network_access = false, exclude_tmpdir_env_var = true } # keep network\n\n',
])
def test_project_toml_write_preserves_comments_unrelated_and_ignored_values_and_unsets_only_selected_fields(catalog, sandbox_table):
    path = project_file(catalog.workspace, '# authored project\nweb_search = "cached" # keep search\nmodel_verbosity = "high"\n\n'
        + sandbox_table +
        '[notes]\nmessage = "preserve me"\n\n[llm.custom]\napi_key = "ignored-project-secret"\n')
    original_user = catalog.user.read_bytes()
    response = save_project(catalog, read(catalog), catalog.workspace, catalog.workspace,
                            web_search="live", model_verbosity=None, network_access=True)
    assert response.status_code == 200, response.text
    written = path.read_text(encoding="utf-8")
    document = tomllib.loads(written)
    assert document["web_search"] == "live"
    assert "model_verbosity" not in document
    assert document["sandbox_workspace_write"] == {"network_access": True, "exclude_tmpdir_env_var": True}
    assert document["notes"] == {"message": "preserve me"}
    assert document["llm"]["custom"]["api_key"] == "ignored-project-secret"
    assert all(comment in written for comment in ("# authored project", "# keep search", "# keep network"))
    after = response.json()
    selected = next(scope for scope in after["scopes"] if scope["kind"] == "project")
    assert selected["version"] == after["write"]["version"]
    assert selected["values"] == {"web_search": "live", "network_access": True}
    assert after["effective"]["model_verbosity"] == "low"
    assert after["write"]["status"] == "applied"
    assert after["write"]["path"] == str(path)
    assert catalog.user.read_bytes() == original_user
    catalog.hook.assert_awaited_once_with(source="project_settings", file_path=str(path))


def test_project_version_is_checked_inside_the_file_transaction_after_hooks(catalog):
    path = project_file(catalog.workspace, 'web_search = "cached"\n')
    before = read(catalog)
    external = 'web_search = "cached"\nmodel_reasoning_summary = "detailed" # external edit\n'

    async def change_during_hook(**_kwargs):
        path.write_text(external, encoding="utf-8")

    catalog.monkeypatch.setattr(service, "run_config_change_hook", change_during_hook)
    response = save_project(catalog, before, catalog.workspace, catalog.workspace, web_search="live")
    assert response.status_code == 409, response.text
    assert "changed since it was read" in response.json()["detail"]
    assert path.read_text(encoding="utf-8") == external


def test_discovered_project_folder_can_create_its_missing_config_without_touching_user_settings(catalog):
    (catalog.workspace / ".minicode").mkdir()
    path = catalog.workspace / ".minicode" / "config.toml"
    original_user = catalog.user.read_bytes()
    response = save_project(catalog, read(catalog), catalog.workspace, catalog.workspace, model_verbosity="high")
    assert response.status_code == 200, response.text
    assert tomllib.loads(path.read_text())["model_verbosity"] == "high"
    assert catalog.user.read_bytes() == original_user


def test_disabled_project_retains_raw_diagnostics_but_cannot_write_or_run_change_hooks(catalog):
    path = project_file(catalog.workspace, 'web_search = "live"\n')
    original = path.read_bytes()
    catalog.monkeypatch.setattr("backend.workspace.trust.is_workspace_trusted", lambda _root: False)
    payload = read(catalog)
    scope = payload["scopes"][0]
    response = save_project(catalog, payload, catalog.workspace, catalog.workspace, web_search="disabled")
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == scope["disabled_reason"]
    assert path.read_bytes() == original
    catalog.hook.assert_not_awaited()


def test_parent_and_child_project_overrides_use_actual_layer_order_and_child_unset_inherits_parent(catalog):
    parent_file = project_file(catalog.workspace, 'model_verbosity = "low"\n')
    child = catalog.workspace / "app"
    child_file = project_file(child, 'model_verbosity = "high" # child\n')
    original_child = child_file.read_bytes()
    response = save_project(catalog, read(catalog, child), child, catalog.workspace, model_verbosity="medium")
    assert response.status_code == 200, response.text
    after = response.json()
    assert after["write"]["status"] == "overridden"
    assert after["write"]["fields"]["model_verbosity"]["overriding_source"]["source"]["project_config_folder"] == str(child_file.parent)
    assert after["effective"]["model_verbosity"] == "high"
    assert tomllib.loads(parent_file.read_text())["model_verbosity"] == "medium"
    assert child_file.read_bytes() == original_child
    response = save_project(catalog, after, child, child, model_verbosity=None)
    assert response.status_code == 200, response.text
    assert response.json()["write"]["status"] == "applied"
    assert response.json()["effective"]["model_verbosity"] == "medium"
    assert "model_verbosity" not in tomllib.loads(child_file.read_text())


def test_project_cannot_bypass_managed_origins_or_requirements_with_null(catalog):
    path = project_file(catalog.workspace, 'sandbox_mode = "read-only"\nweb_search = "live"\n')
    (catalog.managed / "managed-settings.json").write_text('{"web_search":"cached"}', encoding="utf-8")
    catalog.requirements.write_text('allowed_sandbox_modes = ["read-only"]\n', encoding="utf-8")
    original = path.read_bytes()
    payload = read(catalog)
    for patch in ({"web_search": "disabled"}, {"sandbox_mode": None}, {"sandbox_mode": "danger-full-access"}):
        response = save_project(catalog, payload, catalog.workspace, catalog.workspace, **patch)
        assert response.status_code == 409, response.text
        assert path.read_bytes() == original
    assert catalog.hook.await_count == 1  # Only unset reaches the transaction to check the inherited effective value.


def test_unregistered_project_scope_does_not_write_an_arbitrary_file(catalog):
    path = project_file(catalog.workspace, 'web_search = "cached"\n')
    original = path.read_bytes()
    response = catalog.client.put("/api/settings/agent-config", json={"workspace_root": str(catalog.workspace),
        "scope_id": f"project:{catalog.home}", "web_search": "live", "path": str(catalog.user)})
    assert response.status_code == 409, response.text
    assert "scope is no longer available" in response.json()["detail"]
    assert path.read_bytes() == original
    catalog.hook.assert_not_awaited()


def test_project_hook_refusal_and_filesystem_failure_preserve_the_actual_file(catalog):
    from backend.hooks.manager import HookResult
    path = project_file(catalog.workspace, 'web_search = "cached" # authored\n')
    original = path.read_bytes()
    before = read(catalog)
    catalog.hook.return_value = HookResult(blocked=True, message="Project change blocked by fixture hook")
    response = save_project(catalog, before, catalog.workspace, catalog.workspace, web_search="live")
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "Project change blocked by fixture hook"
    assert path.read_bytes() == original
    catalog.hook.return_value = None

    def denied(*_args, **_kwargs):
        raise OSError("fixture project disk write rejected")

    catalog.monkeypatch.setattr(service, "atomic_write_text", denied)
    response = save_project(catalog, before, catalog.workspace, catalog.workspace, web_search="live")
    assert response.status_code == 500, response.text
    assert response.json()["detail"] == "fixture project disk write rejected"
    assert path.read_bytes() == original
