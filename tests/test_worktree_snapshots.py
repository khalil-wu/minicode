"""End-to-end tests for worktree snapshot/restore (recovery safety).

These run against a real git repo so the snapshot commit + ref + restore
behaviour is exercised for real, not mocked. Skipped when git is unavailable.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from backend.workspace.worktree import WorktreeManager
from backend.workspace.worktree_snapshots import WorktreeSnapshotRecord, WorktreeSnapshotStore

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test User")
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-m", "init")


def _make_manager(tmp_path: Path) -> tuple[WorktreeManager, Path]:
    repo = tmp_path / "repo"
    _init_repo(repo)
    store = WorktreeSnapshotStore(tmp_path / "snaps")
    return WorktreeManager(repo, snapshot_store=store), repo


def test_safe_remove_snapshots_dirty_worktree_then_restores(tmp_path: Path) -> None:
    manager, repo = _make_manager(tmp_path)

    wt = repo / ".minicode" / "worktrees" / "conv_x"
    wt.parent.mkdir(parents=True, exist_ok=True)
    assert manager.create_worktree(wt, branch="minicode/conv_x", new_branch=True) is True

    # Make it dirty: modify a tracked file AND add an untracked file.
    (wt / "README.md").write_text("hello\nmodified\n", encoding="utf-8")
    (wt / "new_file.txt").write_text("brand new\n", encoding="utf-8")

    # Dirty + no force => refused, nothing destroyed.
    blocked = manager.safe_remove_worktree(wt, force=False)
    assert blocked.removed is False
    assert blocked.needs_force is True
    assert wt.exists()

    # Dirty + force => snapshot taken, then removed.
    removal = manager.safe_remove_worktree(
        wt, force=True, conversation_id="conv_x", branch="minicode/conv_x"
    )
    assert removal.removed is True
    assert removal.snapshot is not None
    snapshot_id = removal.snapshot.id

    # The anchoring ref must exist in the main repo and survive worktree removal.
    ref_check = subprocess.run(
        ["git", "rev-parse", "--verify", removal.snapshot.snapshot_ref],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert ref_check.returncode == 0
    assert not wt.exists()

    # Snapshot metadata is persisted and discoverable.
    assert manager.list_snapshots("conv_x")

    # Restore brings back both the tracked modification and the untracked file.
    result = manager.restore_snapshot(snapshot_id)
    assert result.restored is True
    assert result.path is not None
    restored = result.path
    assert (restored / "README.md").read_text(encoding="utf-8") == "hello\nmodified\n"
    assert (restored / "new_file.txt").read_text(encoding="utf-8") == "brand new\n"


def test_safe_remove_clean_worktree_skips_snapshot(tmp_path: Path) -> None:
    manager, repo = _make_manager(tmp_path)

    wt = repo / ".minicode" / "worktrees" / "conv_clean"
    wt.parent.mkdir(parents=True, exist_ok=True)
    assert manager.create_worktree(wt, branch="minicode/conv_clean", new_branch=True) is True

    removal = manager.safe_remove_worktree(wt, force=False)
    assert removal.removed is True
    assert removal.snapshot is None
    assert not wt.exists()


def test_restore_removed_worktree_recreates_branch_and_dirty_files(tmp_path: Path) -> None:
    manager, repo = _make_manager(tmp_path)
    branch = "minicode/conv_rollback"
    wt = repo / ".minicode" / "worktrees" / "conv_rollback"
    wt.parent.mkdir(parents=True, exist_ok=True)
    assert manager.create_worktree(wt, branch=branch, new_branch=True) is True

    (wt / "README.md").write_text("rollback tracked\n", encoding="utf-8")
    (wt / "rollback-untracked.txt").write_text("rollback untracked\n", encoding="utf-8")
    removal = manager.safe_remove_worktree(
        wt,
        force=True,
        conversation_id="conv_rollback",
        branch=branch,
    )
    assert removal.removed is True
    assert removal.head
    assert removal.snapshot is not None

    restored = manager.restore_removed_worktree(
        wt,
        branch=branch,
        expected_head=removal.head,
        snapshot_id=removal.snapshot.id,
    )

    assert restored.restored is True
    assert restored.path == wt.resolve()
    assert subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=wt,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip() == branch
    assert subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=wt,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip() == removal.head
    assert (wt / "README.md").read_text(encoding="utf-8") == "rollback tracked\n"
    assert (wt / "rollback-untracked.txt").read_text(encoding="utf-8") == "rollback untracked\n"
    assert subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=wt,
        capture_output=True,
        text=True,
        check=True,
    ).stdout == ""


def test_restore_removed_worktree_refuses_branch_that_moved_after_cleanup(tmp_path: Path) -> None:
    manager, repo = _make_manager(tmp_path)
    branch = "minicode/conv_moved"
    wt = repo / ".minicode" / "worktrees" / "conv_moved"
    wt.parent.mkdir(parents=True, exist_ok=True)
    assert manager.create_worktree(wt, branch=branch, new_branch=True) is True
    removal = manager.safe_remove_worktree(wt, force=False, branch=branch)
    assert removal.removed is True
    assert removal.head

    (repo / "README.md").write_text("main advanced\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "advance main")
    advanced_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    _git(repo, "branch", "-f", branch, advanced_head)

    restored = manager.restore_removed_worktree(
        wt,
        branch=branch,
        expected_head=removal.head,
    )

    assert restored.restored is False
    assert "branch moved" in str(restored.error).lower()
    assert not wt.exists()


def test_snapshot_does_not_modify_real_index(tmp_path: Path) -> None:
    manager, repo = _make_manager(tmp_path)
    wt = repo / ".minicode" / "worktrees" / "conv_index"
    wt.parent.mkdir(parents=True, exist_ok=True)
    assert manager.create_worktree(wt, branch="minicode/conv_index", new_branch=True) is True

    (wt / "README.md").write_text("unstaged change\n", encoding="utf-8")
    (wt / "new.txt").write_text("untracked\n", encoding="utf-8")
    before = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=wt,
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    record = manager.snapshot_worktree(wt, conversation_id="conv_index")
    after = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=wt,
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    assert record is not None
    assert before == ""
    assert after == before


def test_safe_remove_refuses_when_dirty_snapshot_fails(monkeypatch, tmp_path: Path) -> None:
    manager, repo = _make_manager(tmp_path)
    wt = repo / ".minicode" / "worktrees" / "conv_snapshot_failure"
    wt.parent.mkdir(parents=True, exist_ok=True)
    assert manager.create_worktree(wt, branch="minicode/conv_snapshot_failure", new_branch=True) is True
    (wt / "README.md").write_text("dirty\n", encoding="utf-8")
    monkeypatch.setattr(manager, "snapshot_worktree", lambda *args, **kwargs: None)

    removal = manager.safe_remove_worktree(wt, force=True, snapshot=True)

    assert removal.removed is False
    assert "snapshot failed" in str(removal.error).lower()
    assert wt.exists()


def test_restore_missing_snapshot_returns_error(tmp_path: Path) -> None:
    manager, _ = _make_manager(tmp_path)
    result = manager.restore_snapshot("wtsnap_does_not_exist")
    assert result.restored is False
    assert result.error


def test_snapshot_store_rejects_path_like_ids_and_filters_by_repo(tmp_path: Path) -> None:
    store = WorktreeSnapshotStore(tmp_path / "snaps")
    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    repo_a.mkdir()
    repo_b.mkdir()
    store.save(
        WorktreeSnapshotRecord(
            id="wtsnap_aaaaaaaaaaaa",
            main_repo_path=str(repo_a),
            snapshot_sha="a" * 40,
        )
    )
    store.save(
        WorktreeSnapshotRecord(
            id="wtsnap_bbbbbbbbbbbb",
            main_repo_path=str(repo_b),
            snapshot_sha="b" * 40,
        )
    )

    assert store.get("../wtsnap_aaaaaaaaaaaa") is None
    assert [record.id for record in store.list(repo_root=repo_a)] == ["wtsnap_aaaaaaaaaaaa"]
    assert not (tmp_path / "wtsnap_aaaaaaaaaaaa.json").exists()


def test_snapshot_pruning_removes_stale_metadata_and_git_ref(tmp_path: Path) -> None:
    manager, repo = _make_manager(tmp_path)
    snapshot_id = "wtsnap_stale000001"
    snapshot_ref = f"refs/minicode/wt-snapshots/{snapshot_id}"
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    _git(repo, "update-ref", snapshot_ref, head)
    manager._snapshots().save(WorktreeSnapshotRecord(
        id=snapshot_id,
        main_repo_path=str(repo),
        snapshot_sha=head,
        snapshot_ref=snapshot_ref,
        created_at="2000-01-01T00:00:00+00:00",
    ))

    assert manager.prune_snapshots(max_records=50, max_age_days=30) == 1
    assert manager._snapshots().get(snapshot_id) is None
    ref_check = subprocess.run(
        ["git", "rev-parse", "--verify", snapshot_ref],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert ref_check.returncode != 0
