from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException, Response
from fastapi.testclient import TestClient

from backend.api import routes_projects
from backend.conversations.repository import ConversationRepository
from backend.workspace import trust
from backend.workspace.project_sources import local_project_source_selection
from backend.workspace.recent_projects import RecentProjectStore
from backend.workspace.service import WorkspaceService


@pytest.fixture
def project_api(monkeypatch, tmp_path):
    state = tmp_path / "state"
    monkeypatch.setattr("backend.workspace.recent_projects.DEFAULT_STORE_PATH", state / "recent_projects.json")
    monkeypatch.setattr(trust, "TRUSTED_WORKSPACES_FILE", state / "trusted_workspaces.json")
    monkeypatch.setattr(routes_projects._state, "ws_manager", SimpleNamespace(iter_sessions=lambda: []))
    app = FastAPI()
    app.include_router(routes_projects.router)
    return TestClient(app), RecentProjectStore(), state


def test_name_only_project_mints_a_real_git_workspace_and_retains_identity_after_source_changes(project_api, tmp_path):
    client, store, state = project_api
    created = client.post("/api/projects", json={"name": "Research without source folders"})
    assert created.status_code == 200, created.text
    project = created.json()["project"]
    root = Path(project["managed_workspace_root"])
    assert project["source_folders"] == []
    assert project["path"] == str(root)
    assert root.is_relative_to(state / "projects")
    assert (root / ".git" / "HEAD").read_text().strip() == "ref: refs/heads/main"
    assert trust.is_workspace_trusted(root)
    assert not trust.is_workspace_trusted(root.parent)
    assert not trust.is_workspace_trusted(state)
    assert local_project_source_selection(root) == (project["path"], (root,))
    from backend.api.routes_local_environments import get_environments
    assert get_environments(Response())["projects"][0]["path"] == str(root)

    service = WorkspaceService(get_workspace_root=lambda: root)
    service.write_file("notes.md", "# Actual project file\n")
    assert service.read_file("notes.md").content == "# Actual project file\n"
    repo = ConversationRepository(tmp_path / "chats")
    chat = repo.create_conversation(workspace_root=str(root), execution_cwd=str(root))
    assert chat.project_id == project["path"] and chat.execution_cwd == str(root)

    source = tmp_path / "selected-source"
    source.mkdir()
    (source / "keep.txt").write_text("Existing user data")
    trust.trust_created_task_directory(source)
    edited = client.patch("/api/projects", json={"path": project["path"], "source_folders": [str(source)], "name": "Renamed"})
    assert edited.status_code == 200, edited.text
    loaded = RecentProjectStore().project_for_root(str(source))
    assert loaded.path == project["path"] and loaded.execution_root == str(source)
    assert loaded.managed_workspace_root == str(root)
    assert get_environments(Response())["projects"][0]["path"] == str(source)
    cleared = client.patch("/api/projects", json={"path": project["path"], "source_folders": []})
    assert cleared.status_code == 200, cleared.text
    restored = RecentProjectStore().project_for_root(str(root))
    assert restored.path == project["path"] and restored.execution_root == str(root)
    assert restored.source_folders == [] and restored.name == "Renamed"
    assert (root / "notes.md").read_text() == "# Actual project file\n"
    assert (source / "keep.txt").read_text() == "Existing user data"
    retained = repo.get_conversation(chat.id)
    assert retained.execution_cwd == str(root) and retained.project_id == project["path"]


def test_clearing_all_sources_allocates_managed_root_without_reusing_the_removed_source(project_api, tmp_path):
    client, store, _ = project_api
    source = tmp_path / "old-source"
    source.mkdir()
    (source / "unsaved.txt").write_text("Never moved or deleted")
    trust.trust_created_task_directory(source)
    original = store.create("Existing named project", [str(source)])
    edited = client.patch("/api/projects", json={"path": original.path, "source_folders": []})
    assert edited.status_code == 200, edited.text
    project = edited.json()["project"]
    managed = Path(project["managed_workspace_root"])
    assert managed != source
    assert project["path"] == original.path and project["source_folders"] == []
    assert RecentProjectStore().project_for_root(str(managed)).path == original.path
    assert RecentProjectStore().project_for_root(str(source)) is None
    assert (source / "unsaved.txt").read_text() == "Never moved or deleted"


def test_git_creation_failure_is_not_registered_or_reported_as_a_project(project_api, monkeypatch):
    client, store, _ = project_api
    monkeypatch.setattr(routes_projects, "run_delivery_command", AsyncMock(side_effect=HTTPException(409, "Actual git initialization failure")))
    response = client.post("/api/projects", json={"name": "Must not pretend to exist"})
    assert response.status_code == 409
    assert response.json()["detail"] == "Actual git initialization failure"
    assert store.list() == []
    assert not trust.TRUSTED_WORKSPACES_FILE.exists()
