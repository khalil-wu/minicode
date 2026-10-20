from pathlib import Path
import subprocess
from types import SimpleNamespace
import pytest

from fastapi import Response

from backend.agent.worktree import create_agent_worktree, cleanup_agent_worktree, resume_agent_worktree
from backend.api.routes_worktree_settings import get_worktree_settings, put_worktree_settings
from backend.conversations.repository import ConversationRepository
from backend.services.conversation_payload_service import create_isolated_worktree_binding, cleanup_isolated_worktree
from backend.services.worktree_settings_service import (
    WorktreePreferences, cleanup_managed_worktrees, is_managed_worktree_path,
    list_managed_worktrees, managed_worktree_root, update_worktree_preferences,
)
from backend.services.workspace_service import main_worktree_root, worktree_has_local_changes
from backend.workspace.worktree import WorktreeManager


@pytest.fixture(autouse=True)
def trusted_repositories(tmp_path, monkeypatch):
    monkeypatch.setattr("backend.workspace.trust.TRUSTED_WORKSPACES_FILE", tmp_path / "trusted-workspaces.json")


def git(root, *args):
    result = subprocess.run(["git", *args], cwd=root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
    return result.stdout


def repository(root):
    root.mkdir()
    git(root, "init")
    git(root, "config", "user.email", "offline@example.invalid")
    git(root, "config", "user.name", "Offline fixture")
    (root / "tracked.txt").write_text("original content\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-m", "Initial fixture")
    from backend.workspace.trust import trust_created_task_directory
    trust_created_task_directory(root)
    return root


def test_root_preference_reaches_real_conversation_and_agent_creation_and_survives_a_root_change(tmp_path):
    repo = repository(tmp_path / "repo")
    defaults = get_worktree_settings(Response())
    assert {key: defaults[key] for key in ("worktree_root", "auto_cleanup_enabled", "keep_count", "upstream_refresh_mode")} == {
        "worktree_root": "", "auto_cleanup_enabled": True, "keep_count": 15, "upstream_refresh_mode": "never"}
    configured = tmp_path / "managed pool"
    put_worktree_settings(WorktreePreferences(worktree_root=str(configured), keep_count=7), Response())
    put_worktree_settings(WorktreePreferences(auto_cleanup_enabled=False), Response())
    saved = get_worktree_settings(Response())
    assert saved["keep_count"] == 7 and saved["worktree_root"] == str(configured)
    assert saved["auto_cleanup_enabled"] is False
    conversations = ConversationRepository(tmp_path / "conversations")
    conversation = conversations.create_conversation(workspace_root=str(repo), git_isolated=True)
    creation = create_isolated_worktree_binding(conversation, current_workspace_root=repo, main_worktree_root=main_worktree_root)
    assert creation.created, creation.error_event
    assert Path(creation.worktree_path).parent == configured
    child, error = create_agent_worktree("subagent-child", repo)
    assert not error and child.worktree_path.parent == configured
    put_worktree_settings(WorktreePreferences(worktree_root=str(tmp_path / "next-pool")), Response())
    assert is_managed_worktree_path(Path(creation.worktree_path), repo)
    resumed = resume_agent_worktree(child.worktree_path, expected_repo_root=repo, expected_subagent_id="subagent-child",
        expected_worktree_branch=child.temporary_branch, expected_head_commit=child.head_commit)
    assert resumed == child
    assert cleanup_agent_worktree(resumed) == (False, "")
    bound = conversations.update_workspace_binding(conversation.id, workspace_root=creation.workspace_root,
        worktree_path=creation.worktree_path, git_branch=creation.git_branch, git_isolated=True)
    cleanup = cleanup_isolated_worktree(bound, current_workspace_root=repo, main_worktree_root=main_worktree_root,
        is_path_within=lambda path, root: path.is_relative_to(root), worktree_has_local_changes=worktree_has_local_changes)
    assert cleanup["removed"], cleanup
    assert list_managed_worktrees(repository=conversations) == []
    legacy = repo / ".minicode" / "worktrees" / "existing-user-tree"
    git(repo, "worktree", "add", "-b", "existing-user-tree", str(legacy))
    existing = conversations.create_conversation(workspace_root=str(legacy), git_isolated=True)
    conversations.update_workspace_binding(existing.id, workspace_root=str(legacy), worktree_path=str(legacy), git_isolated=True)
    row = list_managed_worktrees(repository=conversations)[0]
    assert row["path"] == str(legacy) and row["auto_cleanup_eligible"] is False
    update_worktree_preferences(WorktreePreferences(auto_cleanup_enabled=True, keep_count=1))
    assert cleanup_managed_worktrees(repository=conversations) == []
    assert legacy.exists()


def test_auto_cleanup_uses_real_snapshots_and_skips_pinned_loaded_and_dirty_worktrees(tmp_path):
    repo = repository(tmp_path / "repo")
    manager = WorktreeManager(repo)
    pool = managed_worktree_root()
    pool.mkdir(parents=True)
    conversations = ConversationRepository(tmp_path / "conversations")
    paths = {}
    for name in ("pinned", "loaded", "clean", "dirty"):
        path = pool / name
        assert manager.create_worktree(path, branch=f"codex/{name}", new_branch=True)
        paths[name] = path
    pinned = conversations.create_conversation(workspace_root=str(paths["pinned"]), git_isolated=True)
    conversations.set_conversation_pinned(pinned.id, True)
    loaded = conversations.create_conversation(workspace_root=str(paths["loaded"]), git_isolated=True)
    clean = conversations.create_conversation(workspace_root=str(paths["clean"]), git_isolated=True)
    conversations.update_workspace_binding(clean.id, workspace_root=str(paths["clean"]), worktree_path=str(paths["clean"]), git_isolated=True)
    (paths["dirty"] / "unsaved-on-disk.txt").write_text("Keep user work\n", encoding="utf-8")
    paths["newest"] = pool / "newest"
    assert manager.create_worktree(paths["newest"], branch="codex/newest", new_branch=True)
    ws_manager = SimpleNamespace(execution_contexts=SimpleNamespace(contexts=lambda: [SimpleNamespace(conversation_id=loaded.id)]),
        resource_sessions=lambda: ())
    update_worktree_preferences(WorktreePreferences(keep_count=1))
    results = cleanup_managed_worktrees(repository=conversations, ws_manager=ws_manager)
    removed = next(result for result in results if result["path"] == str(paths["clean"]))
    assert removed["removed"] and removed["snapshot_id"]
    snapshot = manager.list_snapshots()[0]
    assert git(repo, "show", f"{snapshot.snapshot_sha}:tracked.txt").strip() == "original content"
    assert not paths["clean"].exists()
    assert all(paths[name].exists() for name in ("pinned", "loaded", "dirty", "newest"))
    assert conversations.get_conversation(clean.id).worktree_path == ""
    assert conversations.get_conversation(clean.id).workspace_root == str(repo)
    assert any(result["path"] == str(paths["dirty"]) and result["error"] == "Worktree has local changes" for result in results)
    update_worktree_preferences(WorktreePreferences(auto_cleanup_enabled=False, keep_count=1))
    assert cleanup_managed_worktrees(repository=conversations, ws_manager=ws_manager) == []


def test_upstream_refresh_setting_reaches_creation_with_real_local_remote(tmp_path):
    repo = repository(tmp_path / "repo")
    branch = git(repo, "branch", "--show-current").strip()
    remote = tmp_path / "origin.git"
    remote.mkdir()
    git(remote, "init", "--bare")
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "--set-upstream", "origin", branch)
    contributor = tmp_path / "contributor"
    git(tmp_path, "clone", str(remote), str(contributor))
    git(contributor, "config", "user.email", "offline@example.invalid")
    git(contributor, "config", "user.name", "Offline fixture")
    (contributor / "new-upstream.txt").write_text("Actual newer remote commit\n", encoding="utf-8")
    git(contributor, "add", ".")
    git(contributor, "commit", "-m", "New upstream work")
    git(contributor, "push", "origin", branch)
    previous = git(repo, "rev-parse", f"refs/remotes/origin/{branch}")
    manager = WorktreeManager(repo)
    pool = managed_worktree_root()
    assert manager.create_worktree(pool / "without-refresh", branch="codex/without-refresh", new_branch=True)
    assert git(repo, "rev-parse", f"refs/remotes/origin/{branch}") == previous
    update_worktree_preferences(WorktreePreferences(upstream_refresh_mode="best-effort"))
    assert manager.create_worktree(pool / "with-refresh", branch="codex/with-refresh", new_branch=True)
    assert git(repo, "rev-parse", f"refs/remotes/origin/{branch}") == git(contributor, "rev-parse", "HEAD")
