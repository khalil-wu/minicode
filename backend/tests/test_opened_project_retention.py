from backend.workspace.recent_projects import RecentProjectStore
from backend.services.workspace_service import list_workspace_recent_payload


def test_opened_projects_survive_reload_and_do_not_evict_or_reorder(tmp_path, monkeypatch):
    file = tmp_path / "projects.json"
    store = RecentProjectStore(file)
    paths = [tmp_path / f"project-{index}" for index in range(25)]
    for path in paths:
        path.mkdir()
        store.add(str(path), path.name)
    store.add(str(paths[0]), "First project")
    paths[-1].rmdir()
    restored = RecentProjectStore(file)
    assert [project.path for project in restored.list()] == [str(path) for path in paths]
    monkeypatch.setattr("backend.workspace.recent_projects.DEFAULT_STORE_PATH", file)
    assert len(list_workspace_recent_payload()["projects"]) == 25
    assert restored.remove(str(paths[0])) is True
    assert len(RecentProjectStore(file).list()) == 24
