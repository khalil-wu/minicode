from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from backend.conversations.repository import ConversationRepository
from backend.runtime_env import sanitized_git_env
from backend.services import workspace_service
from backend.services.conversation_worktree_handoff_service import restore_main_checkout, switch_main_checkout
from backend.services.worktree_settings_service import WorktreePreferences, update_worktree_preferences
from backend.workspace.worktree import WorktreeManager
from backend.ws.handlers import conversation as conversation_handlers


def git(root: Path, *args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=root, env=sanitized_git_env(), capture_output=True, check=True).stdout


@pytest.mark.parametrize("binding_fails", [False, True])
@pytest.mark.parametrize("split_index", [False, True])
def test_handoff_keeps_ignored_files_and_recovers_them_on_rollback(tmp_path, monkeypatch, binding_fails, split_index):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "Handoff Test")
    git(root, "config", "user.email", "handoff@example.invalid")
    (root / ".gitignore").write_text("private.txt\n", encoding="utf-8")
    (root / "tracked.txt").write_text("base\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-m", "base")
    worktree = root / ".minicode" / "worktrees" / "handoff"
    worktree.parent.mkdir(parents=True)
    git(root, "worktree", "add", "-b", "feature", str(worktree))
    (worktree / "private.txt").write_bytes(b"ignored user bytes\r\n")
    (worktree / "tracked.txt").write_bytes(b"staged change\n")
    git(worktree, "add", "tracked.txt")
    if split_index:
        git(worktree, "update-index", "--split-index")
    (worktree / "tracked.txt").write_bytes(b"working change\n")

    def readonly_git(path, *args, timeout=5, **_kwargs):
        assert Path(path).resolve().is_relative_to(tmp_path.resolve())
        return subprocess.run(["git", "--no-optional-locks", *args], cwd=path, env=sanitized_git_env(),
                              capture_output=True, text=True, encoding="utf-8", timeout=timeout)

    monkeypatch.setattr(workspace_service, "run_readonly_git", readonly_git)
    repository = ConversationRepository(base_dir=tmp_path / "conversations")
    record = repository.create_conversation(conversation_id="handoff-test", workspace_root=str(worktree),
                                            git_branch="feature", worktree_path=str(worktree), git_isolated=True)
    if binding_fails:
        monkeypatch.setattr(repository, "update_workspace_binding", lambda *_args, **_kwargs: None)
    session = SimpleNamespace(conversation_repo=repository, main_worktree_root=lambda _path: root,
        attachment_store=SimpleNamespace(share_for_conversation=Mock()),
        artifact_store=SimpleNamespace(share_for_conversation=Mock()), emit_command_result=AsyncMock())
    monkeypatch.setattr(conversation_handlers, "_release_active_sessions_from_conversation_workspace", AsyncMock(return_value=[]))
    monkeypatch.setattr(conversation_handlers, "_switch_active_sessions_to_conversation_workspace", AsyncMock(return_value=[]))
    monkeypatch.setattr(conversation_handlers, "_broadcast_conversation_lists", AsyncMock(return_value=[]))

    asyncio.run(conversation_handlers._handle_conversation_worktree_handoff_claimed(
        session, conversation=record, conversation_id=record.id, target_kind="local", dirty_action="stash",
        preflight={"source": {"path": str(worktree)}, "main_checkout": {"branch": "main", "head": git(root, "rev-parse", "HEAD").decode().strip()}},
    ))
    result = session.emit_command_result.await_args.kwargs["data"]
    snapshot_id = result["worktree_snapshot_id"]
    assert snapshot_id and result["worktree_snapshot_ref"]
    if binding_fails:
        assert result["rollback_completed"] is True
        assert (worktree / "private.txt").read_bytes() == b"ignored user bytes\r\n"
        assert (worktree / "tracked.txt").read_bytes() == b"working change\n"
        assert git(worktree, "show", ":tracked.txt") == b"staged change\n"
        if split_index:
            assert git(worktree, "rev-parse", "--shared-index-path").strip()
        assert git(root, "branch", "--show-current").strip() == b"main"
    else:
        assert result["completed"] is True
        assert (root / "tracked.txt").read_bytes() == b"working change\n"
        assert git(root, "show", ":tracked.txt") == b"staged change\n"
        if split_index:
            assert git(root, "rev-parse", "--shared-index-path").strip()
        snapshot = WorktreeManager(root).restore_snapshot(snapshot_id)
        assert snapshot.restored, snapshot.error
        assert (snapshot.path / "private.txt").read_bytes() == b"ignored user bytes\r\n"


@pytest.mark.parametrize("restore", ["switch", "branch", "detached"])
def test_handoff_switch_never_overwrites_destination_ignored_file(tmp_path, restore):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "Handoff Test")
    git(root, "config", "user.email", "handoff@example.invalid")
    (root / ".gitignore").write_text("private.txt\n", encoding="utf-8")
    git(root, "add", ".gitignore")
    git(root, "commit", "-m", "base")
    git(root, "switch", "-c", "feature")
    (root / "private.txt").write_bytes(b"tracked in feature\n")
    git(root, "add", "-f", "private.txt")
    git(root, "commit", "-m", "feature")
    head = git(root, "rev-parse", "HEAD").decode().strip()
    git(root, "switch", "main")
    (root / "private.txt").write_bytes(b"ignored user bytes\r\n")

    if restore == "switch":
        switched, _error = switch_main_checkout(root, "feature")
    elif restore == "branch":
        switched, _error = restore_main_checkout(root, branch="feature")
    else:
        switched, _error = restore_main_checkout(root, head=head)
    assert not switched
    assert (root / "private.txt").read_bytes() == b"ignored user bytes\r\n"
    assert git(root, "branch", "--show-current").strip() == b"main"


@pytest.mark.parametrize("binding_fails", [False, True])
@pytest.mark.parametrize("source_in_subdir", [False, True])
def test_local_to_worktree_preserves_raw_changes_without_touching_ignored_repositories(tmp_path, monkeypatch, binding_fails, source_in_subdir):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "Handoff Test")
    git(root, "config", "user.email", "handoff@example.invalid")
    git(root, "config", "core.autocrlf", "true")
    if source_in_subdir:
        git(root, "config", "filter.case.clean", "tr a-z A-Z")
        git(root, "config", "filter.case.smudge", "tr A-Z a-z")
        git(root, "config", "filter.case.required", "true")
        (root / ".gitattributes").write_bytes(b"*.txt filter=case\ntracked.mask filter=case\n")
    (root / ".gitignore").write_bytes(b"private.txt\nnested-repo/\ntracked.mask\n")
    (root / "tracked.txt").write_bytes(b"base\n")
    (root / "tracked.mask").write_bytes(b"base mask\n")
    (root / "subdir").mkdir()
    (root / "subdir" / "kept.txt").write_bytes(b"subdirectory\n")
    git(root, "add", ".gitignore", "tracked.txt", "subdir/kept.txt", *([".gitattributes"] if source_in_subdir else []))
    git(root, "add", "-f", "tracked.mask")
    git(root, "commit", "-m", "base")
    (root / "tracked.txt").write_bytes(b"staged source\n")
    (root / "tracked.mask").write_bytes(b"staged mask\n")
    git(root, "add", "tracked.txt", "tracked.mask")
    git(root, "update-index", "--split-index")
    (root / "tracked.txt").write_bytes(b"working source\n")
    (root / "tracked.mask").write_bytes(b"working mask\n")
    (root / "new.txt").write_bytes(b"untracked source\n")
    (root / "private.txt").write_bytes(b"ignored source\r\n")
    nested = root / "nested-repo"
    nested.mkdir()
    git(nested, "init", "-q")
    (nested / ".git" / "private-data").write_bytes(b"nested metadata")
    update_worktree_preferences(WorktreePreferences(worktree_root=str(tmp_path / "managed")))

    def readonly_git(path, *args, timeout=5, **_kwargs):
        assert Path(path).resolve().is_relative_to(tmp_path.resolve())
        return subprocess.run(["git", "--no-optional-locks", *args], cwd=path, env=sanitized_git_env(),
                              capture_output=True, text=True, encoding="utf-8", timeout=timeout)

    monkeypatch.setattr(workspace_service, "run_readonly_git", readonly_git)
    repository = ConversationRepository(base_dir=tmp_path / "conversations")
    source = root / "subdir" if source_in_subdir else root
    record = repository.create_conversation(conversation_id="handoff-main", workspace_root=str(source))
    if binding_fails:
        monkeypatch.setattr(repository, "update_workspace_binding", lambda *_args, **_kwargs: None)
    session = SimpleNamespace(conversation_repo=repository, main_worktree_root=lambda _path: root,
        attachment_store=SimpleNamespace(share_for_conversation=Mock()),
        artifact_store=SimpleNamespace(share_for_conversation=Mock()), emit_command_result=AsyncMock())
    monkeypatch.setattr(conversation_handlers, "_release_active_sessions_from_conversation_workspace", AsyncMock(return_value=[]))
    monkeypatch.setattr(conversation_handlers, "_switch_active_sessions_to_conversation_workspace", AsyncMock(return_value=[]))
    monkeypatch.setattr(conversation_handlers, "_broadcast_conversation_lists", AsyncMock(return_value=[]))
    asyncio.run(conversation_handlers._handle_conversation_worktree_handoff_claimed(
        session, conversation=record, conversation_id=record.id, target_kind="worktree", dirty_action="stash",
        preflight={"source": {"path": str(source)}, "main_checkout": {"branch": "main", "head": git(root, "rev-parse", "HEAD").decode().strip()}},
    ))
    result = session.emit_command_result.await_args.kwargs["data"]
    assert result["worktree_snapshot_id"]
    destination = root if binding_fails else Path(result["workspace_root"])
    if binding_fails:
        assert result["rollback_completed"] is True
    else:
        assert result["completed"] is True
    assert (destination / "tracked.txt").read_bytes() == b"working source\n"
    assert (destination / "tracked.mask").read_bytes() == b"working mask\n"
    assert (destination / "new.txt").read_bytes() == b"untracked source\n"
    assert git(destination, "show", ":tracked.txt") == (b"STAGED SOURCE\n" if source_in_subdir else b"staged source\n")
    assert git(destination, "show", ":tracked.mask") == (b"STAGED MASK\n" if source_in_subdir else b"staged mask\n")
    assert git(destination, "rev-parse", "--shared-index-path").strip()
    assert (root / "private.txt").read_bytes() == b"ignored source\r\n"
    assert (nested / ".git" / "private-data").read_bytes() == b"nested metadata"
    if not binding_fails:
        assert not (destination / "private.txt").exists()
        assert not (destination / "nested-repo").exists()
