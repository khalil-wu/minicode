from __future__ import annotations

from types import SimpleNamespace

from backend.services.conversation_payload_service import copy_worktree_includes
from backend.services.conversation_worktree_handoff_service import build_handoff_preflight


def test_worktree_include_copies_only_allowlisted_non_symlink_files(tmp_path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    (source / ".env.local").write_text("TOKEN=test", encoding="utf-8")
    (source / "config").mkdir()
    (source / "config" / "runtime.json").write_text("{}", encoding="utf-8")
    (source / ".worktreeinclude").write_text(".env.local\nconfig\n../outside\n", encoding="utf-8")

    copied, skipped = copy_worktree_includes(source, destination)

    assert copied == [".env.local", "config"]
    assert "../outside" in skipped
    assert (destination / ".env.local").read_text(encoding="utf-8") == "TOKEN=test"
    assert (destination / "config" / "runtime.json").exists()


def test_handoff_preflight_allows_explicit_stash_for_dirty_source(tmp_path, monkeypatch) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    conversation = SimpleNamespace(
        id="conv_handoff123",
        git_isolated=False,
        workspace_root=str(root),
        worktree_path="",
        git_branch="",
        archived=False,
    )

    monkeypatch.setattr(
        "backend.services.conversation_worktree_handoff_service._status",
        lambda _path: " M changed.py",
    )
    monkeypatch.setattr(
        "backend.services.conversation_worktree_handoff_service._head",
        lambda _path: "abc123",
    )
    monkeypatch.setattr(
        "backend.services.conversation_worktree_handoff_service._branch",
        lambda _path: "main",
    )
    monkeypatch.setattr(
        "backend.services.conversation_worktree_handoff_service._ignored_sample",
        lambda _path: [],
    )
    monkeypatch.setattr(
        "backend.services.conversation_worktree_handoff_service._git",
        lambda *_args: (False, ""),
    )
    repository = SimpleNamespace(list_conversations=lambda: [conversation])

    preflight = build_handoff_preflight(
        conversation,
        target="worktree",
        conversation_repo=repository,
        main_worktree_root=lambda _path: root,
        has_running_turn=False,
        dirty_action="stash",
    )

    assert preflight["allowed"] is True
    assert preflight["dirty_action"] == "stash"
    assert preflight["main_checkout"] == {
        "path": str(root),
        "branch": "main",
        "head": "abc123",
    }
    assert any(check["code"] == "source.dirty.stash" for check in preflight["checks"])
