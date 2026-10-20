from __future__ import annotations
import asyncio
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api import routes_projects
from backend.conversations.repository import ConversationRepository
from backend.workspace.recent_projects import RecentProjectStore
from backend.workspace.project_sources import local_project_source_roots


def setup_store(monkeypatch, tmp_path):
    path = tmp_path / "state" / "recent_projects.json"
    monkeypatch.setattr("backend.workspace.recent_projects.DEFAULT_STORE_PATH", path)
    monkeypatch.setattr(routes_projects, "is_workspace_trusted", lambda root: root.is_dir())
    monkeypatch.setattr("backend.workspace.project_sources.is_workspace_trusted", lambda root: root.is_dir())
    return RecentProjectStore(path)


def test_named_project_reopening_secondary_preserves_name_folders_pin_marker_and_legacy_records(monkeypatch, tmp_path):
    store = setup_store(monkeypatch, tmp_path)
    first, second, old = [tmp_path / name for name in ("first", "second", "legacy")]
    for folder in (first, second, old): folder.mkdir()
    store.add(str(old), "Original legacy")
    store.add(str(first), "first")
    appearance = {"color": "blue", "marker": {"kind": "icon", "icon": "book"}}
    project = store.create("Named research", [str(first), str(second)], appearance)
    store.update(project.path, {"pinned": False})
    before = json.loads(store._store_path.read_text(encoding="utf-8"))
    store.add(str(second), "second", "python")
    loaded = RecentProjectStore(store._store_path).project_for_root(str(second))
    assert loaded.name == "Named research" and loaded.source_folders == [str(first), str(second)]
    assert loaded.pinned is False and loaded.appearance == appearance
    assert len(store.list()) == 2
    assert store.list()[0].to_dict() == before[0]
    edited = store.update(project.path, {"source_folders": [str(second)], "name": "Renamed"})
    assert edited.path == str(first) and edited.source_folders == [str(second)]
    repo = ConversationRepository(tmp_path / "chats")
    chat = repo.create_conversation(workspace_root=str(second), execution_cwd=str(second))
    assert chat.project_id == str(first) and chat.execution_cwd == str(second)


def test_create_and_edit_project_api_registers_multiple_folders_without_changing_files_or_opening_a_chat(monkeypatch, tmp_path):
    store = setup_store(monkeypatch, tmp_path)
    folders = [tmp_path / "A", tmp_path / "B"]
    for folder in folders: folder.mkdir(); (folder / "keep.txt").write_text("untouched")
    published = []
    async def send(payload, **_kwargs): published.append(payload)
    monkeypatch.setattr(routes_projects._state, "ws_manager", SimpleNamespace(iter_sessions=lambda: [SimpleNamespace(is_connected=True, send_payload=send)]))
    app = FastAPI(); app.include_router(routes_projects.router)
    with TestClient(app) as client:
        result = client.post("/api/projects", json={"name": "Folder collection", "source_folders": list(map(str, folders))})
        assert result.status_code == 200
        project = result.json()["project"]
        assert project["name"] == "Folder collection" and project["source_folders"] == list(map(str, folders))
        updated = client.patch("/api/projects", json={"path": project["path"], "pinned": False,
            "appearance": {"color": "purple", "marker": {"kind": "emoji", "emoji": "🧠"}}})
        assert updated.status_code == 200 and updated.json()["project"]["pinned"] is False
        assert client.get("/api/projects").json()["projects"][0]["appearance"]["marker"]["emoji"] == "🧠"
        assert client.patch("/api/projects", json={"path": project["path"], "source_folders": None}).status_code == 422
    assert len(published) == 2 and all(event["type"] == "workspace.recent.list" for event in published)
    assert [folder.joinpath("keep.txt").read_text() for folder in folders] == ["untouched", "untouched"]
    assert store.list()[0].source_folders == list(map(str, folders))


def test_untrusted_source_folder_is_rejected_without_registering_partial_project(monkeypatch, tmp_path):
    store = setup_store(monkeypatch, tmp_path)
    first, other = tmp_path / "first", tmp_path / "untrusted"
    first.mkdir(); other.mkdir()
    monkeypatch.setattr(routes_projects, "is_workspace_trusted", lambda root: root == first)
    app = FastAPI(); app.include_router(routes_projects.router)
    with TestClient(app) as client:
        response = client.post("/api/projects", json={"name": "Denied", "source_folders": [str(first), str(other)]})
    assert response.status_code == 403 and store.list() == []


def test_registered_project_folders_reach_the_actual_executor_permission_policy(monkeypatch, tmp_path):
    from backend.agent.environment_runtime import resolve_environment_config, thread_environment_defaults
    from backend.agent.environment_selection import TurnEnvironmentRequest, TurnEnvironmentSelection
    from backend.config_layers import ConfigLayer, ConfigLayerSource, ConfigLayerStack
    from backend.permissions.context import PermissionContext
    from backend.permissions.environment_scope import EnvironmentPermissionScope
    from backend.workspace.executor_local_policy import local_sandbox_policy
    store = setup_store(monkeypatch, tmp_path)
    first, second, outside = [tmp_path / name for name in ("A", "B", "outside")]
    for folder in (first, second, outside): folder.mkdir()
    store.create("Two roots", [str(first), str(second)])
    roots = local_project_source_roots(first)
    selected = TurnEnvironmentSelection.from_request(TurnEnvironmentRequest("local", first.as_uri(), tuple(root.as_uri() for root in roots)))
    stack = ConfigLayerStack((ConfigLayer(ConfigLayerSource("user"), {"sandbox_workspace_write": {"exclude_tmpdir_env_var": True, "exclude_slash_tmp": True}}),))
    defaults = thread_environment_defaults(PermissionContext(mode="confirm"), stack)
    platform = "windows" if os.name == "nt" else "linux"
    config = asyncio.run(resolve_environment_config(selected, defaults.for_selection(selected), SimpleNamespace(filesystem=object()), {"platformOs": platform}))
    scope = EnvironmentPermissionScope("local", config.permission_profile, selected.cwd, config.workspace_roots, platform)
    resolved = local_sandbox_policy(scope.sandbox_context()).resolve()
    assert resolved.resolve_access(first / "new.txt").can_write
    assert resolved.resolve_access(second / "new.txt").can_write
    assert not resolved.resolve_access(outside / "new.txt").can_write
    monkeypatch.setattr("backend.workspace.project_sources.is_workspace_trusted", lambda root: False)
    assert local_project_source_roots(first) == (first,)


def test_existing_local_attachment_refreshes_project_roots_but_preserves_host_owned_config(tmp_path):
    from backend.agent.environment_runtime import prepare_thread_environments
    from backend.agent.environment_selection import EnvironmentConfig, EnvironmentConfigState, ThreadEnvironmentDefaults, TurnEnvironmentRequest, TurnEnvironmentSelection
    from backend.environments.manager import EnvironmentManager
    from backend.sandbox.policy import PermissionProfile
    first, second, host_root = (tmp_path / name for name in ("A", "B", "host"))
    for folder in (first, second, host_root): folder.mkdir()
    root = first.as_uri()
    defaults = ThreadEnvironmentDefaults(EnvironmentConfig(PermissionProfile.disabled(), (root,)), "disabled")
    local = SimpleNamespace(filesystem=object(), process=object(), info=AsyncMock(return_value={"platformOs": "windows" if os.name == "nt" else "linux"}), close=AsyncMock())
    async def run():
        manager = EnvironmentManager(tmp_path / "config", local_backend=local, environ={})
        owner = None
        try:
            owner = await prepare_thread_environments(manager=manager, defaults=defaults, cwd_uri=root,
                workspace_root_uris=(root, second.as_uri()), project_source_cwd=root)
            async with await owner.snapshot() as snapshot:
                assert snapshot.primary().config.workspace_roots == (root, second.as_uri())
            owner = await prepare_thread_environments(manager=manager, defaults=defaults, cwd_uri=root,
                workspace_root_uris=(root,), owner=owner)
            async with await owner.snapshot() as snapshot:
                assert snapshot.primary().config.workspace_roots == (root,)
            host = EnvironmentConfig(PermissionProfile.disabled(), (host_root.as_uri(),))
            await owner.update_selections((TurnEnvironmentSelection.from_request(TurnEnvironmentRequest("local", root,
                (host_root.as_uri(),), EnvironmentConfigState.ready(host))),))
            await prepare_thread_environments(manager=manager, defaults=defaults, cwd_uri=root, workspace_root_uris=(root, second.as_uri()), owner=owner, project_source_cwd=root)
            async with await owner.snapshot() as snapshot:
                assert snapshot.primary().config.workspace_roots == (host_root.as_uri(),)
        finally:
            if owner is not None: await owner.aclose()
            await manager.close()
    asyncio.run(run())
