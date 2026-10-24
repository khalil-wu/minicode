import subprocess

import pytest
from fastapi.testclient import TestClient

from backend.main import app
from backend.workspace.file_review import file_review_diff, file_review_options


def git(root, *args):
    return subprocess.run(["git", "--literal-pathspecs", *args], cwd=root, check=True,
        capture_output=True, text=True, encoding="utf-8").stdout.strip()


@pytest.fixture
def repo(tmp_path):
    repository = tmp_path / "repo"
    repository.mkdir()
    git(repository, "init", "-b", "main")
    git(repository, "config", "user.email", "files@example.test")
    git(repository, "config", "user.name", "Files fixture")
    (repository / "staged.txt").write_text("base\n", encoding="utf-8")
    (repository / "unstaged.txt").write_text("base\n", encoding="utf-8")
    git(repository, "add", ".")
    git(repository, "commit", "-m", "base")
    git(repository, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(repository, "remote", "add", "origin", "https://example.invalid/repo.git")
    git(repository, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    git(repository, "checkout", "-b", "feature")
    (repository / "committed.txt").write_text("committed change\n", encoding="utf-8")
    git(repository, "add", ".")
    git(repository, "commit", "-m", "feature commit")
    git(repository, "checkout", "main")
    (repository / "upstream.txt").write_text("upstream change\n", encoding="utf-8")
    git(repository, "add", ".")
    git(repository, "commit", "-m", "upstream commit")
    git(repository, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(repository, "checkout", "feature")
    return repository


@pytest.mark.asyncio
async def test_file_review_sources_use_their_actual_comparison_and_literal_paths(repo):
    (repo / "staged.txt").write_text("staged change\n", encoding="utf-8")
    git(repo, "add", "staged.txt")
    (repo / "unstaged.txt").write_text("working change\n", encoding="utf-8")
    literal = "new [literal] # file.txt"
    (repo / literal).write_text("untracked change\n", encoding="utf-8")
    staged = await file_review_diff(repo, source="staged")
    unstaged = await file_review_diff(repo, source="unstaged")
    uncommitted = await file_review_diff(repo, source="uncommitted")
    branch = await file_review_diff(repo, source="branch", base_branch="origin/main")
    commit = await file_review_diff(repo, source="commit", commit_sha=git(repo, "rev-parse", "HEAD"))
    paths = lambda result: {file["path"] for file in result["files"]}
    assert paths(staged) == {"staged.txt"}
    assert paths(unstaged) == {"unstaged.txt", literal}
    assert paths(uncommitted) == {"staged.txt", "unstaged.txt", literal}
    assert paths(branch) == {"committed.txt", "staged.txt", "unstaged.txt", literal}
    assert paths(commit) == {"committed.txt"}
    assert branch["comparison"]["merge_base_sha"] == git(repo, "merge-base", "HEAD", "origin/main")
    assert branch["comparison"]["merge_base_sha"] != git(repo, "rev-parse", "origin/main")
    options = await file_review_options(repo, base_branch="origin/main")
    assert options["default_base_branch"] == "origin/main"
    assert [item["subject"] for item in options["commits"]] == ["feature commit"]


@pytest.mark.asyncio
async def test_file_review_nonrepo_and_unborn_head_are_real_empty_states(tmp_path):
    directory = tmp_path / "unborn-repo"
    directory.mkdir()
    assert (await file_review_options(directory))["is_git_repo"] is False
    assert (await file_review_diff(directory, source="uncommitted"))["files"] == []
    git(directory, "init", "-b", "main")
    (directory / "first.txt").write_text("first\n", encoding="utf-8")
    result = await file_review_diff(directory, source="uncommitted")
    assert result["is_git_repo"] is True
    assert [item["path"] for item in result["files"]] == ["first.txt"]


def test_home_git_http_keeps_trust_and_ownerless_uri_boundaries(monkeypatch, tmp_path):
    monkeypatch.setattr("backend.workspace.trust.is_workspace_trusted", lambda _root: False)
    with TestClient(app) as client:
        for route in ("branches", "diff?source=uncommitted"):
            response = client.get(f"/api/workspace/git/{route}", params={"workspace_root": str(tmp_path)})
            assert response.status_code == 403
        response = client.get("/api/workspace/git/diff", params={"workspace_root": "file:///home/repo", "source": "uncommitted"})
        assert response.status_code == 409


def test_home_git_http_reads_selected_repo_and_full_patch_without_changing_active_root(monkeypatch, repo, tmp_path):
    from backend.workspace import state as workspace_state
    from backend.workspace.state import get_active_workspace_root, set_active_workspace_root
    other = tmp_path / "other"
    other.mkdir()
    runtime_state = tmp_path / "runtime-state"
    runtime_state.mkdir()
    monkeypatch.setattr("backend.workspace.trust.is_workspace_trusted", lambda _root: True)
    monkeypatch.setattr(workspace_state, "WORKSPACE_STATE_FILE", runtime_state / "active-workspace.json")
    monkeypatch.setattr(workspace_state, "_active_workspace_root", None)
    set_active_workspace_root(other)
    with TestClient(app) as client:
        options = client.get("/api/workspace/git/branches", params={"workspace_root": str(repo), "base_branch": "origin/main"})
        comparison = client.get("/api/workspace/git/diff", params={"workspace_root": str(repo), "source": "branch", "base_branch": "origin/main"})
        patch = client.get("/api/workspace/git/diff", params={"workspace_root": str(repo), "source": "branch", "base_branch": "origin/main", "for_apply": True})
    assert options.status_code == comparison.status_code == patch.status_code == 200
    assert options.json()["default_base_branch"] == "origin/main"
    assert [file["path"] for file in comparison.json()["files"]] == ["committed.txt"]
    assert "+committed change" in patch.json()["diff"]
    assert get_active_workspace_root() == other


def test_git_http_selected_subdirectory_uses_relative_rows_and_repository_patch_paths(monkeypatch, repo):
    nested = repo / "nested"
    nested.mkdir()
    (nested / "item.txt").write_text("saved\n", encoding="utf-8")
    git(repo, "add", "nested/item.txt")
    git(repo, "commit", "-m", "nested file")
    (nested / "item.txt").write_text("staged child\n", encoding="utf-8")
    git(repo, "add", "nested/item.txt")
    monkeypatch.setattr("backend.workspace.trust.is_workspace_trusted", lambda _root: True)
    with TestClient(app) as client:
        params = {"workspace_root": str(repo), "path": str(nested), "source": "staged"}
        rows = client.get("/api/workspace/git/diff", params=params)
        patch = client.get("/api/workspace/git/diff", params={**params, "for_apply": True})
    assert rows.status_code == patch.status_code == 200
    assert [file["path"] for file in rows.json()["files"]] == ["item.txt"]
    assert "diff --git a/nested/item.txt b/nested/item.txt" in patch.json()["diff"]
