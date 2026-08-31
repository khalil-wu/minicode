from pathlib import Path
import asyncio
from types import SimpleNamespace
from unittest.mock import ANY, patch

from backend.agent.run_context import RunContext
from backend.hooks.manager import HookEvent, HookResult
from backend.tools import worktree_tools
from backend.tools.worktree_tools import CreateWorktreeTool, RemoveWorktreeTool
from backend.workspace.worktree import WorktreeInfo, WorktreeManager, summarize_worktree_status


def test_summarize_worktree_status_detects_linked_worktree(tmp_path: Path) -> None:
    main_repo = tmp_path / "repo"
    linked_worktree = tmp_path / "repo-feature"
    main_repo.mkdir()
    linked_worktree.mkdir()
    (main_repo / ".git").mkdir()
    (linked_worktree / ".git").write_text("gitdir: ../repo/.git/worktrees/repo-feature", encoding="utf-8")

    status = summarize_worktree_status(
        linked_worktree,
        [
          WorktreeInfo(
              path=main_repo,
              branch="main",
              commit="1234567890abcdef",
              is_bare=False,
              is_detached=False,
          ),
          WorktreeInfo(
              path=linked_worktree,
              branch="feature/runtime-ui",
              commit="abcdef1234567890",
              is_bare=False,
              is_detached=False,
          ),
        ],
    )

    assert status.is_worktree is True
    assert status.main_repo_path == main_repo.resolve()
    assert status.current_path == linked_worktree.resolve()
    assert status.current_branch == "feature/runtime-ui"
    assert status.worktree_count == 2


def test_summarize_worktree_status_marks_primary_checkout_as_non_worktree(tmp_path: Path) -> None:
    main_repo = tmp_path / "repo"
    main_repo.mkdir()
    (main_repo / ".git").mkdir()

    status = summarize_worktree_status(
        main_repo,
        [
          WorktreeInfo(
              path=main_repo,
              branch="main",
              commit="1234567890abcdef",
              is_bare=False,
              is_detached=False,
          ),
        ],
    )

    assert status.is_worktree is False
    assert status.main_repo_path == main_repo.resolve()
    assert status.current_path == main_repo.resolve()
    assert status.current_branch == "main"
    assert status.worktree_count == 1


def test_worktree_manager_create_and_remove_use_isolated_branch_commands(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    target = tmp_path / "repo" / ".minicode" / "worktrees" / "conv_123"
    with patch.object(WorktreeManager, "_is_git_repo", return_value=True):
        manager = WorktreeManager(repo)

    with patch("backend.workspace.worktree.subprocess.run") as run:
        run.return_value.stdout = ""
        assert manager.create_worktree(target, branch="minicode/conv_123", new_branch=True) is True
        run.assert_called_with(
            ["git", "worktree", "add", "-b", "minicode/conv_123", str(target.resolve())],
            cwd=repo.resolve(),
            env=ANY,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
            timeout=ANY,
        )

    with patch("backend.workspace.worktree.subprocess.run") as run:
        run.return_value.stdout = ""
        assert manager.remove_worktree(target, force=True) is True
        run.assert_called_with(
            ["git", "worktree", "remove", "--force", str(target.resolve())],
            cwd=repo.resolve(),
            env=ANY,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
            timeout=ANY,
        )


def test_hook_owned_worktree_works_without_git_and_routes_remove_back_to_hook(monkeypatch, tmp_path: Path) -> None:
    created_path = (tmp_path / "hook-worktree").resolve()
    calls: list[tuple[str, dict[str, str]]] = []

    class _HookManager:
        def has_hooks(self, event):
            return event in {HookEvent.WORKTREE_CREATE, HookEvent.WORKTREE_REMOVE}

        async def run_worktree_create(self, **kwargs):
            calls.append(("create", dict(kwargs)))
            return HookResult(worktree_path=str(created_path))

        async def run_worktree_remove(self, **kwargs):
            calls.append(("remove", dict(kwargs)))
            return HookResult()

    worktree_tools._HOOK_CREATED_WORKTREES.clear()
    context = SimpleNamespace(
        workspace_root=tmp_path,
        run_context=RunContext(hook_manager=_HookManager()),
    )

    async def run() -> None:
        created = await CreateWorktreeTool().execute(
            {"path": "requested-name", "branch": "feature-x"},
            context=context,
        )
        assert created.is_error is False
        assert str(created_path) in created.content

        removed = await RemoveWorktreeTool().execute(
            {"path": str(created_path)},
            context=context,
        )
        assert removed.is_error is False

    asyncio.run(run())
    assert [name for name, _ in calls] == ["create", "remove"]
    assert calls[0][1]["branch"] == "feature-x"
    assert calls[1][1]["path"] == str(created_path)
    assert str(created_path) not in worktree_tools._HOOK_CREATED_WORKTREES
