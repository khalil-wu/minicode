import os

import pytest
from fastapi.testclient import TestClient

from backend.main import app
from backend.workspace.state import clear_active_workspace_root, set_active_workspace_root


def _workspace_params(workspace_root, **values):
    return {"workspace_root": str(workspace_root), **values}


@pytest.fixture(autouse=True)
def _active_workspace(monkeypatch, tmp_path):
    from backend.workspace import state as workspace_state

    monkeypatch.setattr(
        "backend.workspace.trust.is_workspace_trusted",
        lambda _path: True,
    )
    monkeypatch.setattr(
        workspace_state,
        "WORKSPACE_STATE_FILE",
        tmp_path / "active_workspace.json",
    )
    workspace_state._active_workspace_root = None  # type: ignore[attr-defined]
    set_active_workspace_root(tmp_path)
    try:
        yield
    finally:
        clear_active_workspace_root()


def test_workspace_tree_lists_visible_entries(monkeypatch, tmp_path) -> None:
    (tmp_path / "src").mkdir(parents=True)
    (tmp_path / "src" / "main.py").write_text("print('ok')\n", encoding="utf-8")
    (tmp_path / ".git").mkdir(parents=True)

    monkeypatch.setattr("backend.main.PROJECT_ROOT", tmp_path)

    with TestClient(app) as client:
        response = client.get(
            "/api/workspace/tree",
            params=_workspace_params(tmp_path, path="."),
        )

    assert response.status_code == 200
    payload = response.json()
    names = {entry["name"] for entry in payload["entries"]}
    assert "src" in names
    assert ".git" not in names


def test_workspace_file_write_and_read_round_trip(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("backend.main.PROJECT_ROOT", tmp_path)

    with TestClient(app) as client:
        write_response = client.put(
            "/api/workspace/file",
            params=_workspace_params(tmp_path),
            json={
                "path": "frontend/src/example.ts",
                "content": "export const demo = 1;\n",
            },
        )
        read_response = client.get(
            "/api/workspace/file",
            params=_workspace_params(tmp_path, path="frontend/src/example.ts"),
        )

    assert write_response.status_code == 200
    write_payload = write_response.json()
    assert write_payload["language_hint"] == "typescript"
    assert write_payload["path"] == "frontend/src/example.ts"

    assert read_response.status_code == 200
    read_payload = read_response.json()
    assert read_payload["content"] == "export const demo = 1;\n"
    assert len(read_payload["content_hash"]) == 64
    assert read_payload["name"] == "example.ts"


def test_workspace_compare_write_rejects_stale_hash(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("backend.main.PROJECT_ROOT", tmp_path)
    target = tmp_path / "src" / "example.ts"
    target.parent.mkdir(parents=True)
    target.write_text("export const value = 1;\n", encoding="utf-8")

    with TestClient(app) as client:
        read_response = client.get(
            "/api/workspace/file",
            params=_workspace_params(tmp_path, path="src/example.ts"),
        )
        original_hash = read_response.json()["content_hash"]

        target.write_text("export const value = 2;\n", encoding="utf-8")

        stale_write_response = client.put(
            "/api/workspace/file/compare-write",
            params=_workspace_params(tmp_path),
            json={
                "path": "src/example.ts",
                "expected_hash": original_hash,
                "content": "export const value = 3;\n",
            },
        )
        fresh_hash = client.get(
            "/api/workspace/file",
            params=_workspace_params(tmp_path, path="src/example.ts"),
        ).json()["content_hash"]
        fresh_write_response = client.put(
            "/api/workspace/file/compare-write",
            params=_workspace_params(tmp_path),
            json={
                "path": "src/example.ts",
                "expected_hash": fresh_hash,
                "content": "export const value = 3;\n",
            },
        )

    assert stale_write_response.status_code == 409
    assert stale_write_response.json()["detail"]["actual_hash"] == fresh_hash
    assert target.read_text(encoding="utf-8") == "export const value = 3;\n"
    assert fresh_write_response.status_code == 200
    assert fresh_write_response.json()["content"] == "export const value = 3;\n"
    assert len(fresh_write_response.json()["content_hash"]) == 64


def test_workspace_path_traversal_is_rejected(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("backend.main.PROJECT_ROOT", tmp_path)

    with TestClient(app) as client:
        response = client.get(
            "/api/workspace/file",
            params=_workspace_params(tmp_path, path="../outside.txt"),
        )

    assert response.status_code == 400
    assert "outside workspace" in response.json()["detail"].lower()


def test_workspace_git_path_outside_active_workspace_is_rejected(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("backend.main.PROJECT_ROOT", tmp_path)
    outside = tmp_path.parent / f"{tmp_path.name}-outside-git"
    outside.mkdir()

    with TestClient(app) as client:
        response = client.get(
            "/api/workspace/git/status",
            params=_workspace_params(tmp_path, path=str(outside)),
        )

    assert response.status_code == 400
    assert "outside workspace" in response.json()["detail"].lower()


def test_workspace_dangerous_files_are_not_readable(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("backend.main.PROJECT_ROOT", tmp_path)
    (tmp_path / ".gitconfig").write_text("[user]\n", encoding="utf-8")
    (tmp_path / ".env.example").write_text("OPENAI_API_KEY=\n", encoding="utf-8")

    with TestClient(app) as client:
        blocked = client.get(
            "/api/workspace/file",
            params=_workspace_params(tmp_path, path=".gitconfig"),
        )
        template_response = client.get(
            "/api/workspace/file",
            params=_workspace_params(tmp_path, path=".env.example"),
        )

    assert blocked.status_code == 403
    assert "protected path" in blocked.json()["detail"].lower()
    assert "credential" not in blocked.json()["detail"].lower()
    assert template_response.status_code == 200
    assert template_response.json()["content"].replace("\r\n", "\n") == "OPENAI_API_KEY=\n"


def test_workspace_credential_files_are_readable(monkeypatch, tmp_path) -> None:
    # MiniCode has no credential-file hard-refuse on read
    # checkReadPermissionForTool); only dangerous config paths are blocked.
    monkeypatch.setattr("backend.main.PROJECT_ROOT", tmp_path)
    (tmp_path / ".env").write_text("OPENAI_API_KEY=secret\n", encoding="utf-8")

    with TestClient(app) as client:
        file_response = client.get(
            "/api/workspace/file",
            params=_workspace_params(tmp_path, path=".env"),
        )

    assert file_response.status_code == 200


def test_workspace_dangerous_files_cannot_be_mutated(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("backend.main.PROJECT_ROOT", tmp_path)
    dangerous = tmp_path / ".gitconfig"
    dangerous.write_text("[user]\n", encoding="utf-8")

    with TestClient(app) as client:
        write_response = client.put(
            "/api/workspace/file",
            params=_workspace_params(tmp_path),
            json={"path": ".gitconfig", "content": "changed\n"},
        )
        rename_response = client.post(
            "/api/workspace/rename",
            params=_workspace_params(tmp_path),
            json={"path": ".gitconfig", "new_path": "notes.txt"},
        )
        delete_response = client.delete(
            "/api/workspace/path",
            params=_workspace_params(tmp_path, path=".gitconfig"),
        )

    assert write_response.status_code == 403
    assert rename_response.status_code == 403
    assert delete_response.status_code == 403
    assert dangerous.read_text(encoding="utf-8") == "[user]\n"
    assert not (tmp_path / "notes.txt").exists()


def test_workspace_rest_mutations_reject_protected_project_paths(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("backend.main.PROJECT_ROOT", tmp_path)
    git_head = tmp_path / ".git" / "HEAD"
    git_head.parent.mkdir()
    git_head.write_text("ref: refs/heads/main\n", encoding="utf-8")
    source = tmp_path / "notes.txt"
    source.write_text("keep\n", encoding="utf-8")

    with TestClient(app) as client:
        write_response = client.put(
            "/api/workspace/file",
            params=_workspace_params(tmp_path),
            json={"path": ".mcp.json", "content": "{}\n"},
        )
        rename_response = client.post(
            "/api/workspace/rename",
            params=_workspace_params(tmp_path),
            # MiniCode's own state directory. `.claude/` is another harness's
            # config dir and is deliberately not in the protected set.
            json={"path": "notes.txt", "new_path": ".minicode/notes.txt"},
        )
        delete_response = client.delete(
            "/api/workspace/path",
            params=_workspace_params(tmp_path, path=".git/HEAD"),
        )

    assert write_response.status_code == 403
    assert rename_response.status_code == 403
    assert delete_response.status_code == 403
    assert not (tmp_path / ".mcp.json").exists()
    assert source.read_text(encoding="utf-8") == "keep\n"
    assert git_head.read_text(encoding="utf-8") == "ref: refs/heads/main\n"


def test_workspace_symlink_cannot_escape_and_delete_only_unlinks_alias(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("backend.main.PROJECT_ROOT", tmp_path)
    outside = tmp_path.parent / f"{tmp_path.name}-outside-secret.txt"
    outside.write_text("secret\n", encoding="utf-8")
    link = tmp_path / "alias.txt"
    try:
        link.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    with TestClient(app) as client:
        read_response = client.get(
            "/api/workspace/file",
            params=_workspace_params(tmp_path, path="alias.txt"),
        )
        delete_response = client.delete(
            "/api/workspace/path",
            params=_workspace_params(tmp_path, path="alias.txt"),
        )

    assert read_response.status_code == 400
    assert delete_response.status_code == 200
    assert not os.path.lexists(link)
    assert outside.read_text(encoding="utf-8") == "secret\n"


def test_workspace_tree_survives_broken_symlink(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("backend.main.PROJECT_ROOT", tmp_path)
    link = tmp_path / "broken-link"
    try:
        link.symlink_to(tmp_path / "missing-target")
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    with TestClient(app) as client:
        response = client.get(
            "/api/workspace/tree",
            params=_workspace_params(tmp_path, path="."),
        )

    assert response.status_code == 200
    entry = next(item for item in response.json()["entries"] if item["name"] == "broken-link")
    assert entry["path"] == "broken-link"
    assert entry["is_dir"] is False


def test_workspace_directory_create_and_rename(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("backend.main.PROJECT_ROOT", tmp_path)

    with TestClient(app) as client:
        create_response = client.post(
            "/api/workspace/directory",
            params=_workspace_params(tmp_path),
            json={"path": "frontend/src/components"},
        )
        rename_response = client.post(
            "/api/workspace/rename",
            params=_workspace_params(tmp_path),
            json={
                "path": "frontend/src/components",
                "new_path": "frontend/src/ui",
            },
        )

    assert create_response.status_code == 200
    assert create_response.json()["is_dir"] is True
    assert create_response.json()["path"] == "frontend/src/components"

    assert rename_response.status_code == 200
    assert rename_response.json()["path"] == "frontend/src/ui"
    assert (tmp_path / "frontend" / "src" / "ui").is_dir()
    assert not (tmp_path / "frontend" / "src" / "components").exists()


def test_workspace_delete_file_and_nonempty_directory(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("backend.main.PROJECT_ROOT", tmp_path)
    (tmp_path / "src").mkdir(parents=True)
    (tmp_path / "src" / "demo.ts").write_text("export const x = 1;\n", encoding="utf-8")
    (tmp_path / "docs").mkdir(parents=True)
    (tmp_path / "docs" / "guide.md").write_text("# guide\n", encoding="utf-8")

    with TestClient(app) as client:
        delete_file_response = client.delete(
            "/api/workspace/path",
            params=_workspace_params(tmp_path, path="src/demo.ts"),
        )
        delete_nonempty_dir_response = client.delete(
            "/api/workspace/path",
            params=_workspace_params(tmp_path, path="docs", recursive=False),
        )
        delete_recursive_dir_response = client.delete(
            "/api/workspace/path",
            params=_workspace_params(tmp_path, path="docs", recursive=True),
        )

    assert delete_file_response.status_code == 200
    assert delete_file_response.json()["is_dir"] is False
    assert not (tmp_path / "src" / "demo.ts").exists()

    assert delete_nonempty_dir_response.status_code == 409
    assert "not empty" in delete_nonempty_dir_response.json()["detail"].lower()

    assert delete_recursive_dir_response.status_code == 200
    assert delete_recursive_dir_response.json()["is_dir"] is True
    assert not (tmp_path / "docs").exists()


def test_workspace_delete_root_is_rejected(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("backend.main.PROJECT_ROOT", tmp_path)

    with TestClient(app) as client:
        response = client.delete(
            "/api/workspace/path",
            params=_workspace_params(tmp_path, path="."),
        )

    assert response.status_code == 400
    assert "workspace root" in response.json()["detail"].lower()


def test_workspace_tree_uses_explicit_root_instead_of_active_global(monkeypatch, tmp_path) -> None:
    project_root = tmp_path / "minicode"
    imported_root = tmp_path / "external-project"
    project_root.mkdir()
    imported_root.mkdir()
    (project_root / "local.py").write_text("print('local')\n", encoding="utf-8")
    (imported_root / "app.py").write_text("print('external')\n", encoding="utf-8")

    monkeypatch.setattr("backend.main.PROJECT_ROOT", project_root)
    set_active_workspace_root(imported_root)

    try:
        with TestClient(app) as client:
            response = client.get(
                "/api/workspace/tree",
                params=_workspace_params(project_root, path="."),
            )
    finally:
        clear_active_workspace_root()

    assert response.status_code == 200
    payload = response.json()
    assert payload["workspace_root"] == str(project_root.resolve())
    assert [entry["name"] for entry in payload["entries"]] == ["local.py"]


def test_workspace_api_requires_an_explicit_workspace_root(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("backend.main.PROJECT_ROOT", tmp_path)
    clear_active_workspace_root()

    with TestClient(app) as client:
        response = client.get("/api/workspace/tree", params={"path": "."})

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"][-1] == "workspace_root"


def test_workspace_root_persists_across_state_reload(monkeypatch, tmp_path) -> None:
    from backend.workspace import state as workspace_state

    workspace_root = tmp_path / "persisted-project"
    workspace_root.mkdir()
    state_file = tmp_path / "active_workspace.json"

    monkeypatch.setattr(workspace_state, "WORKSPACE_STATE_FILE", state_file, raising=False)

    set_active_workspace_root(workspace_root)
    workspace_state._active_workspace_root = None  # type: ignore[attr-defined]

    loaded = workspace_state.get_active_workspace_root(tmp_path / "fallback")

    assert loaded == workspace_root.resolve()
    assert state_file.exists()
    clear_active_workspace_root()


def test_missing_persisted_workspace_falls_back_to_default(monkeypatch, tmp_path) -> None:
    from backend.workspace import state as workspace_state

    missing_root = tmp_path / "deleted-project"
    fallback = tmp_path / "fallback-project"
    fallback.mkdir()
    state_file = tmp_path / "active_workspace.json"

    monkeypatch.setattr(workspace_state, "WORKSPACE_STATE_FILE", state_file, raising=False)
    set_active_workspace_root(missing_root)
    workspace_state._active_workspace_root = None  # type: ignore[attr-defined]

    loaded = workspace_state.get_active_workspace_root(fallback)

    assert loaded == fallback.resolve()
    assert not state_file.exists()
    clear_active_workspace_root()


def test_workspace_validate_returns_normalized_path(monkeypatch, tmp_path) -> None:
    project_dir = tmp_path / "demo-project"
    project_dir.mkdir(parents=True)
    monkeypatch.setattr("backend.main.PROJECT_ROOT", tmp_path)

    with TestClient(app) as client:
        response = client.post(
            "/api/workspace/validate",
            json={"path": str(project_dir)},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["valid"] is True
    assert payload["normalized_path"] == str(project_dir.resolve())


def test_workspace_fuzzy_search_returns_ranked_matches(monkeypatch, tmp_path) -> None:
    (tmp_path / "frontend" / "src" / "components").mkdir(parents=True)
    (tmp_path / "frontend" / "src" / "AppV2.tsx").write_text("export default null;\n", encoding="utf-8")
    (tmp_path / "frontend" / "src" / "components" / "AppShell.tsx").write_text(
        "export const AppShell = () => null;\n",
        encoding="utf-8",
    )
    (tmp_path / "frontend" / "src" / "app_v2_helper.test.ts").write_text(
        "export const helper = true;\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("backend.main.PROJECT_ROOT", tmp_path)

    with TestClient(app) as client:
        response = client.get(
            "/api/workspace/search",
            params=_workspace_params(
                tmp_path,
                query="App",
                limit=5,
                include_tests=False,
            ),
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["query"] == "App"
    assert payload["results"][0]["path"] == "frontend/src/AppV2.tsx"
    assert payload["results"][0]["name"] == "AppV2.tsx"
    assert payload["results"][0]["score"] > 0
    assert "frontend/src/components/AppShell.tsx" in {
        result["path"] for result in payload["results"]
    }
    assert "frontend/src/app_v2_helper.test.ts" not in {
        result["path"] for result in payload["results"]
    }


def test_workspace_fuzzy_search_honors_gitignore(monkeypatch, tmp_path) -> None:
    (tmp_path / "visible").mkdir()
    (tmp_path / "ignored").mkdir()
    (tmp_path / "visible" / "service-key-notes.txt").write_text("safe\n", encoding="utf-8")
    (tmp_path / "ignored" / "service-key.txt").write_text("ignored\n", encoding="utf-8")
    # MiniCode has no credential-file hard-refuse in search,
    # so a .pem stays searchable; only gitignore excludes it here.
    (tmp_path / "visible" / "service-key.pem").write_text("secret\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("ignored/\n", encoding="utf-8")
    monkeypatch.setattr("backend.main.PROJECT_ROOT", tmp_path)

    with TestClient(app) as client:
        response = client.get(
            "/api/workspace/search",
            params=_workspace_params(tmp_path, query="servicekey", limit=20),
        )

    assert response.status_code == 200
    paths = {item["path"] for item in response.json()["results"]}
    assert "visible/service-key-notes.txt" in paths
    assert "ignored/service-key.txt" not in paths
    assert "visible/service-key.pem" in paths


def test_workspace_recent_project_can_be_removed(monkeypatch, tmp_path) -> None:
    from backend.workspace import recent_projects

    project_dir = tmp_path / "demo-project"
    other_dir = tmp_path / "other-project"
    project_dir.mkdir(parents=True)
    other_dir.mkdir(parents=True)
    store_path = tmp_path / "recent_projects.json"

    monkeypatch.setattr(recent_projects, "DEFAULT_STORE_PATH", store_path)

    store = recent_projects.RecentProjectStore()
    store.add(str(project_dir), "demo-project", "python")
    store.add(str(other_dir), "other-project", "node")

    with TestClient(app) as client:
        response = client.delete(
            "/api/workspace/recent",
            params={"path": str(project_dir)},
        )
        list_response = client.get("/api/workspace/recent")

    assert response.status_code == 200
    assert response.json() == {
        "removed": True,
        "path": str(project_dir.resolve()),
    }
    assert [project["path"] for project in list_response.json()["projects"]] == [
        str(other_dir.resolve())
    ]
