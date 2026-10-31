from __future__ import annotations

import os
import asyncio
import subprocess
from pathlib import Path

import pytest

from backend.workspace.worktree import WorktreeManager
from backend.workspace.worktree_snapshots import WorktreeSnapshotStore


def git(root: Path, *args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, check=True).stdout


def repository(tmp_path: Path) -> tuple[WorktreeManager, Path, Path]:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.name", "Recovery Test")
    git(root, "config", "user.email", "recovery@example.invalid")
    (root / ".gitattributes").write_bytes(b"*.txt text eol=lf filter=recovery\n")
    (root / ".gitignore").write_bytes(b"private*\n")
    (root / "tracked.txt").write_bytes(b"initial\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "baseline")
    manager = WorktreeManager(root, snapshot_store=WorktreeSnapshotStore(tmp_path / "snapshots"),
                              git_runner=lambda *a, index_file=None, **kw: subprocess.run(*a, **kw))
    worktree = root / ".minicode" / "worktrees" / "recovery"
    worktree.parent.mkdir(parents=True)
    assert manager.create_worktree(worktree, branch="recovery", new_branch=True, run_environment=False)
    return manager, root, worktree


@pytest.mark.parametrize("recovery", ["snapshot", "rollback"])
def test_raw_snapshot_preserves_filtered_ignored_large_and_staged_files(tmp_path, recovery):
    manager, root, worktree = repository(tmp_path)
    tracked = worktree / "tracked.txt"
    tracked.write_bytes(b"staged value\n")
    git(worktree, "add", "tracked.txt")
    tracked.write_bytes(b"working value\r\n")
    original_staged = git(worktree, "show", ":tracked.txt")
    index_path = Path(git(worktree, "rev-parse", "--path-format=absolute", "--git-path", "index").decode().strip())
    original_index = index_path.read_bytes()
    data = (b"line one\r\nline two\r\n" * (25 * 1024 * 1024 // 20 + 1))
    (worktree / "private, large.txt").write_bytes(data)
    git(root, "config", "filter.recovery.clean", "git --version")
    git(root, "config", "filter.recovery.smudge", "git --version")
    git(root, "config", "filter.recovery.required", "true")

    removed = manager.safe_remove_worktree(worktree, force=True, branch="recovery")
    assert removed.removed and removed.snapshot is not None
    assert git(root, "show", removed.snapshot.index_sha + ":index") == original_index
    if recovery == "snapshot":
        restored = manager.restore_snapshot(removed.snapshot.id)
    else:
        restored = manager.restore_removed_worktree(worktree, branch="recovery", expected_head=removed.head,
                                                   snapshot_id=removed.snapshot.id)
    assert restored.restored, restored.error
    assert restored.path is not None
    assert (restored.path / "tracked.txt").read_bytes() == b"working value\r\n"
    assert (restored.path / "private, large.txt").read_bytes() == data
    if recovery == "rollback":
        assert git(restored.path, "show", ":tracked.txt") == original_staged
        assert git(restored.path, "branch", "--show-current").strip() == b"recovery"


def test_snapshot_does_not_run_a_required_clean_filter(tmp_path):
    manager, root, worktree = repository(tmp_path)
    (worktree / "tracked.txt").write_bytes(b"unfiltered\r\n")
    git(root, "config", "filter.recovery.clean", "false")
    git(root, "config", "filter.recovery.smudge", "false")
    git(root, "config", "filter.recovery.required", "true")

    snapshot = manager.snapshot_worktree(worktree, branch="recovery")
    assert snapshot is not None
    assert git(root, "show", snapshot.snapshot_sha + ":tracked.txt") == b"unfiltered\r\n"
    assert manager.remove_worktree(worktree, force=True, run_environment=False)
    restored = manager.restore_snapshot(snapshot.id)
    assert restored.restored, restored.error
    assert (restored.path / "tracked.txt").read_bytes() == b"unfiltered\r\n"


def test_snapshot_rejects_embedded_repository_without_removing_it(tmp_path):
    manager, _root, worktree = repository(tmp_path)
    embedded = worktree / "private-embedded"
    embedded.mkdir()
    git(embedded, "init", "-q")
    git(embedded, "config", "user.name", "Embedded")
    git(embedded, "config", "user.email", "embedded@example.invalid")
    (embedded / "secret.txt").write_bytes(b"must remain")
    git(embedded, "add", "secret.txt")
    git(embedded, "commit", "-qm", "embedded")

    removed = manager.safe_remove_worktree(worktree, force=True, branch="recovery")
    assert not removed.removed
    assert (embedded / "secret.txt").read_bytes() == b"must remain"
    assert (embedded / ".git").is_dir()


def test_clean_worktree_with_nested_git_metadata_is_not_removed(tmp_path):
    manager, _root, worktree = repository(tmp_path)
    nested = worktree / "tracked-directory"
    nested.mkdir()
    (nested / "tracked.txt").write_bytes(b"tracked\n")
    git(worktree, "add", "tracked-directory/tracked.txt")
    git(worktree, "commit", "-qm", "tracked directory")
    git(nested, "init", "-q")
    (nested / ".git" / "private-data").write_bytes(b"keep nested metadata")
    assert not manager.has_local_changes(worktree)

    removed = manager.safe_remove_worktree(worktree, force=False, branch="recovery")
    assert not removed.removed and "embedded" in removed.error
    assert (nested / ".git" / "private-data").read_bytes() == b"keep nested metadata"


def test_legacy_model_removal_uses_recovery_snapshot(tmp_path, monkeypatch):
    from backend.permissions.context import PermissionContext, ToolExecutionContext
    from backend.tools import worktree_tools

    manager, root, worktree = repository(tmp_path)
    (worktree / "private.txt").write_bytes(b"ignored data\r\n")

    async def bound_manager(_context):
        return manager

    monkeypatch.setattr(worktree_tools, "_resolve_worktree_manager", bound_manager)
    monkeypatch.setattr(worktree_tools, "_owned_git_worktree_path", lambda _manager, _path: worktree)
    context = ToolExecutionContext(permission=PermissionContext(), workspace_root=root, conversation_id="model")

    async def run():
        refused = await worktree_tools.RemoveWorktreeTool().execute({"path": str(worktree)}, context)
        assert refused.is_error and worktree.exists()
        removed = await worktree_tools.RemoveWorktreeTool().execute({"path": str(worktree), "force": True}, context)
        assert not removed.is_error and "快照 ID" in removed.content

    asyncio.run(run())
    snapshot = manager.list_snapshots("model")[0]
    restored = manager.restore_snapshot(snapshot.id)
    assert restored.restored, restored.error
    assert (restored.path / "private.txt").read_bytes() == b"ignored data\r\n"


def test_failed_restore_keeps_partial_worktree(tmp_path, monkeypatch):
    manager, _root, worktree = repository(tmp_path)
    (worktree / "tracked.txt").write_bytes(b"recovery bytes\n")
    removed = manager.safe_remove_worktree(worktree, force=True, branch="recovery")
    assert removed.removed and removed.snapshot is not None
    original_git_ok = manager._git_ok

    def fail_checkout(cwd, *args):
        if "checkout-index" in args:
            (cwd / "user-created.txt").write_bytes(b"keep partial data")
            return False
        return original_git_ok(cwd, *args)

    monkeypatch.setattr(manager, "_git_ok", fail_checkout)
    restored = manager.restore_removed_worktree(worktree, branch="recovery", expected_head=removed.head,
                                               snapshot_id=removed.snapshot.id)
    assert not restored.restored and restored.path == worktree.resolve()
    assert (worktree / "user-created.txt").read_bytes() == b"keep partial data"


def test_in_progress_merge_is_retained_even_after_resolving_files(tmp_path):
    manager, root, worktree = repository(tmp_path)
    git(root, "checkout", "-qb", "other-side")
    (root / "tracked.txt").write_bytes(b"other side\n")
    git(root, "commit", "-qam", "other side")
    (worktree / "tracked.txt").write_bytes(b"our side\n")
    git(worktree, "commit", "-qam", "our side")
    merge = subprocess.run(["git", "merge", "other-side"], cwd=worktree, capture_output=True)
    assert merge.returncode != 0
    unmerged = git(worktree, "ls-files", "--unmerged", "-z")
    conflict = (worktree / "tracked.txt").read_bytes()
    assert unmerged and b"<<<<<<<" in conflict

    removed = manager.safe_remove_worktree(worktree, force=True, branch="recovery")
    assert not removed.removed and "in-progress Git operation" in removed.error
    assert git(worktree, "ls-files", "--unmerged", "-z") == unmerged
    assert (worktree / "tracked.txt").read_bytes() == conflict
    git(worktree, "checkout", "--ours", "tracked.txt")
    git(worktree, "add", "tracked.txt")
    assert not git(worktree, "status", "--porcelain")
    removed = manager.safe_remove_worktree(worktree, force=False, branch="recovery")
    assert not removed.removed and "in-progress Git operation" in removed.error
    assert worktree.exists()


def test_rollback_restores_split_index_and_staged_content(tmp_path):
    manager, _root, worktree = repository(tmp_path)
    (worktree / "tracked.txt").write_bytes(b"staged split index\n")
    git(worktree, "add", "tracked.txt")
    git(worktree, "update-index", "--split-index")
    assert git(worktree, "rev-parse", "--shared-index-path").strip()
    (worktree / "tracked.txt").write_bytes(b"working version\r\n")

    removed = manager.safe_remove_worktree(worktree, force=True, branch="recovery")
    assert removed.removed and removed.snapshot is not None
    restored = manager.restore_removed_worktree(worktree, branch="recovery", expected_head=removed.head,
                                               snapshot_id=removed.snapshot.id)
    assert restored.restored, restored.error
    assert git(worktree, "show", ":tracked.txt") == b"staged split index\n"
    assert git(worktree, "rev-parse", "--shared-index-path").strip()
    assert (worktree / "tracked.txt").read_bytes() == b"working version\r\n"


def test_raw_snapshot_preserves_real_symbolic_link_when_available(tmp_path):
    manager, _root, worktree = repository(tmp_path)
    try:
        os.symlink("tracked.txt", worktree / "link")
    except OSError as exc:
        pytest.skip(f"Cannot create a symbolic link in this environment: {exc}")
    removed = manager.safe_remove_worktree(worktree, force=True, branch="recovery")
    assert removed.removed and removed.snapshot is not None
    restored = manager.restore_snapshot(removed.snapshot.id)
    assert restored.restored, restored.error
    assert (restored.path / "link").is_symlink()
    assert os.readlink(restored.path / "link") == "tracked.txt"
