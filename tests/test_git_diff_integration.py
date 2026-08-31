from __future__ import annotations

import asyncio
import subprocess

import pytest

from backend.diff.git_integration import GitCommandError, get_working_tree_diff


def test_git_diff_uses_supported_safety_flags_and_returns_changes(tmp_path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "MiniCode test"], cwd=tmp_path, check=True)
    file_path = tmp_path / "tracked.txt"
    file_path.write_text("before\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=tmp_path, check=True)
    file_path.write_text("before\nafter\n", encoding="utf-8")

    result = asyncio.run(get_working_tree_diff(str(tmp_path)))

    assert result.files
    assert result.raw
    assert result.files[0].path == "tracked.txt"
    assert result.total_additions == 1


def test_git_diff_failure_is_structured_and_not_an_empty_success(tmp_path) -> None:
    with pytest.raises(GitCommandError) as caught:
        asyncio.run(get_working_tree_diff(str(tmp_path)))

    error = caught.value
    assert error.exit_code != 0
    assert error.stderr
    assert "git diff" in str(error)


def _init_repo(tmp_path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "MiniCode test"], cwd=tmp_path, check=True)


def test_git_diff_tool_hides_denied_files_but_keeps_renegated_ones(tmp_path) -> None:
    """A denylist negation must not be swallowed by a broader deny pattern.

    The default denylist denies ``.env.*`` and then re-allows ``.env.example``.
    Translating the raw patterns into git pathspecs lost the negation, so a
    tracked ``.env.example`` change was silently missing from every bare
    git_diff while the permission checker considered it readable.
    """

    from backend.config import PermissionSettings
    from backend.permissions.checker import PermissionChecker
    from backend.tools.git_tools import GitDiffTool

    _init_repo(tmp_path)
    for name in (".env", ".env.example", "keep.txt"):
        (tmp_path / name).write_text("before\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=tmp_path, check=True)
    for name in (".env", ".env.example", "keep.txt"):
        (tmp_path / name).write_text("before\nafter\n", encoding="utf-8")

    checker = PermissionChecker(PermissionSettings())
    assert checker.is_path_allowed(".env.example", context=None) is True
    assert checker.is_path_allowed(".env", context=None) is False

    class _Ctx:
        workspace_root = tmp_path
        permission = None
        permission_checker = checker
        cancel_event = None

    result = asyncio.run(GitDiffTool().execute({}, context=_Ctx()))

    assert not result.is_error
    assert "diff --git a/.env.example" in result.content
    assert "diff --git a/keep.txt" in result.content
    assert "diff --git a/.env\n" not in result.content
    assert "diff --git a/.env " not in result.content
