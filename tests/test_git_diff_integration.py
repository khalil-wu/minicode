from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

from backend.diff.git_integration import GitCommandError, get_working_tree_diff
from backend.diff.git_integration import get_staged_diff, get_untracked_files, stage_file, unstage_all, unstage_file
from backend.services.workspace_api_service import workspace_git_diff_payload, workspace_git_status_payload
from backend.workspace.worktree import WorktreeManager


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


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True, encoding="utf-8",
    ).stdout


@pytest.fixture
def audit_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _init_repo(root)
    _git(root, "config", "commit.gpgsign", "false")
    for name in ("plain.txt", "报告 文档.txt", "wild[1].txt", "wild1.txt", "old name.txt"):
        (root / name).write_text("before\n", encoding="utf-8")
    _git(root, "add", "--all")
    _git(root, "commit", "-qm", "initial")
    return root


def test_workspace_status_preserves_unicode_and_rename_destinations(audit_repo: Path) -> None:
    (audit_repo / "报告 文档.txt").write_text("updated\n", encoding="utf-8")
    _git(audit_repo, "mv", "old name.txt", "new name.txt")
    (audit_repo / "新文件夹").mkdir()
    (audit_repo / "新文件夹" / "file.txt").write_text("new\n", encoding="utf-8")

    result = workspace_git_status_payload(audit_repo)

    assert result["modified"] == ["报告 文档.txt"]
    assert result["staged"] == ["new name.txt"]
    assert result["untracked"] == ["新文件夹/file.txt"]
    assert "error" not in result


def test_workspace_git_failure_is_not_reported_as_clean(tmp_path: Path) -> None:
    result = workspace_git_status_payload(tmp_path)
    diff = workspace_git_diff_payload(tmp_path, "")

    assert "not a git repository" in result["error"].lower()
    assert "not a git repository" in diff["error"].lower()


def test_workspace_diff_uses_literal_pathspecs(audit_repo: Path) -> None:
    (audit_repo / "wild[1].txt").write_text("selected file\n", encoding="utf-8")
    (audit_repo / "wild1.txt").write_text("other file\n", encoding="utf-8")

    result = workspace_git_diff_payload(audit_repo, "wild[1].txt")

    assert "+selected file" in result["diff"]
    assert "+other file" not in result["diff"]
    assert "error" not in result


def test_workspace_diff_includes_untracked_files(audit_repo: Path) -> None:
    (audit_repo / "未跟踪.txt").write_text("new content\n", encoding="utf-8")

    selected = workspace_git_diff_payload(audit_repo, "未跟踪.txt")
    combined = workspace_git_diff_payload(audit_repo, "")

    assert "+new content" in selected["diff"]
    assert "+new content" in combined["diff"]
    assert asyncio.run(get_untracked_files(str(audit_repo))) == ["未跟踪.txt"]


def test_git_panels_keep_subdirectory_paths_scoped_and_actionable(audit_repo: Path) -> None:
    nested = audit_repo / "package"
    nested.mkdir()
    name = "工作 文件.txt"
    (nested / name).write_text("before\n", encoding="utf-8")
    _git(audit_repo, "add", "--all")
    _git(audit_repo, "commit", "-qm", "nested fixture")
    (nested / name).write_text("inside change\n", encoding="utf-8")
    (audit_repo / "plain.txt").write_text("outside change\n", encoding="utf-8")
    (nested / "new.txt").write_text("inside untracked\n", encoding="utf-8")
    (audit_repo / "outside-new.txt").write_text("outside untracked\n", encoding="utf-8")

    status = workspace_git_status_payload(nested)
    assert status["modified"] == [name]
    assert status["untracked"] == ["new.txt"]
    diff = asyncio.run(get_working_tree_diff(str(nested)))
    assert [file.path for file in diff.files] == [name]
    combined = workspace_git_diff_payload(nested, "")["diff"]
    assert "+inside change" in combined and "+inside untracked" in combined
    assert "outside" not in combined

    assert asyncio.run(stage_file(str(nested), diff.files[0].path)) is True
    assert [file.path for file in asyncio.run(get_staged_diff(str(nested))).files] == [name]
    assert workspace_git_status_payload(nested)["staged"] == [name]
    assert asyncio.run(unstage_file(str(nested), name)) is True
    assert workspace_git_status_payload(nested)["modified"] == [name]


def test_workspace_diff_and_unstage_work_before_the_first_commit(tmp_path: Path) -> None:
    root = tmp_path / "unborn"
    root.mkdir()
    _init_repo(root)
    (root / "first.txt").write_text("staged\n", encoding="utf-8")
    _git(root, "add", "first.txt")
    (root / "first.txt").write_text("latest working content\n", encoding="utf-8")

    result = workspace_git_diff_payload(root, "first.txt")

    assert "+latest working content" in result["diff"]
    assert "error" not in result
    assert workspace_git_status_payload(root)["branch"] == _git(root, "branch", "--show-current").strip()
    assert asyncio.run(unstage_file(str(root), "first.txt")) is True
    assert _git(root, "ls-files") == ""
    assert (root / "first.txt").read_text(encoding="utf-8") == "latest working content\n"
    _git(root, "add", "first.txt")
    assert asyncio.run(unstage_all(str(root))) is True
    assert _git(root, "ls-files") == ""


def test_diff_reads_do_not_invoke_configured_external_helpers(audit_repo: Path, tmp_path: Path) -> None:
    marker = tmp_path / "external-called"
    helper = tmp_path / "external.py"
    helper.write_text(f"from pathlib import Path\nPath({str(marker)!r}).write_text('called')\n", encoding="utf-8")
    _git(audit_repo, "config", "diff.external", f'"{sys.executable}" "{helper}"')
    (audit_repo / "plain.txt").write_text("updated\n", encoding="utf-8")

    rest = workspace_git_diff_payload(audit_repo, "plain.txt")
    streamed = asyncio.run(get_working_tree_diff(str(audit_repo)))

    assert "+updated" in rest["diff"]
    assert "+updated" in streamed.raw
    assert not marker.exists()


def test_structured_diff_keeps_unicode_and_renamed_paths(audit_repo: Path) -> None:
    (audit_repo / "报告 文档.txt").write_text("updated\n", encoding="utf-8")
    _git(audit_repo, "mv", "old name.txt", "已重命名 文档.txt")

    working = asyncio.run(get_working_tree_diff(str(audit_repo)))
    staged = asyncio.run(get_staged_diff(str(audit_repo)))

    assert [item.path for item in working.files] == ["报告 文档.txt"]
    assert [item.path for item in staged.files] == ["已重命名 文档.txt"]
    assert working.total_additions == 1
    assert working.total_deletions == 1
    assert working.raw.startswith("diff --git ")


def test_staging_and_unstaging_only_affect_the_selected_literal_filename(audit_repo: Path) -> None:
    for name in ("wild[1].txt", "wild1.txt"):
        (audit_repo / name).write_text("updated\n", encoding="utf-8")

    assert asyncio.run(stage_file(str(audit_repo), "wild[1].txt")) is True
    assert _git(audit_repo, "diff", "--cached", "--name-only", "-z").split("\0") == ["wild[1].txt", ""]
    _git(audit_repo, "add", "wild1.txt")
    assert asyncio.run(unstage_file(str(audit_repo), "wild[1].txt")) is True
    assert _git(audit_repo, "diff", "--cached", "--name-only", "-z").split("\0") == ["wild1.txt", ""]


def test_structured_diff_preserves_conflicted_files(audit_repo: Path) -> None:
    branch = _git(audit_repo, "branch", "--show-current").strip()
    _git(audit_repo, "checkout", "-qb", "conflict-side")
    (audit_repo / "plain.txt").write_text("side version\n", encoding="utf-8")
    _git(audit_repo, "commit", "-qam", "side")
    _git(audit_repo, "checkout", "-q", branch)
    (audit_repo / "plain.txt").write_text("main version\n", encoding="utf-8")
    _git(audit_repo, "commit", "-qam", "main")
    merged = subprocess.run(["git", "merge", "conflict-side"], cwd=audit_repo, capture_output=True)
    assert merged.returncode == 1

    result = asyncio.run(get_working_tree_diff(str(audit_repo)))
    staged = asyncio.run(get_staged_diff(str(audit_repo)))

    assert [item.path for item in result.files] == ["plain.txt"]
    assert "<<<<<<<" in result.files[0].patch
    assert [item.path for item in staged.files] == ["plain.txt"]
    assert staged.files[0].patch == "* Unmerged path plain.txt\n"

    # Resolving the content without staging it still leaves an unmerged entry.
    (audit_repo / "plain.txt").write_text("main version\n", encoding="utf-8")
    (audit_repo / "wild1.txt").write_text("ordinary change\n", encoding="utf-8")
    resolved = asyncio.run(get_working_tree_diff(str(audit_repo)))
    by_path = {item.path: item.patch for item in resolved.files}
    assert by_path["plain.txt"] == "* Unmerged path plain.txt\n"
    assert "+ordinary change" in by_path["wild1.txt"]
    assert "Unmerged" not in by_path["wild1.txt"]


def test_structured_diff_groups_both_halves_of_a_file_type_change(audit_repo: Path) -> None:
    blob = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"], cwd=audit_repo,
        input="link-target", capture_output=True, text=True, check=True,
    ).stdout.strip()
    _git(audit_repo, "update-index", "--cacheinfo", f"120000,{blob},报告 文档.txt")
    (audit_repo / "wild1.txt").write_text("ordinary change\n", encoding="utf-8")
    _git(audit_repo, "add", "wild1.txt")

    result = asyncio.run(get_staged_diff(str(audit_repo)))

    by_path = {item.path: item for item in result.files}
    assert set(by_path) == {"报告 文档.txt", "wild1.txt"}
    changed = by_path["报告 文档.txt"]
    assert "-before" in changed.patch
    assert "+link-target" in changed.patch
    assert changed.additions == changed.deletions == 1
    assert "+ordinary change" in by_path["wild1.txt"].patch


def test_worktree_listing_preserves_unicode_directory_names(audit_repo: Path, tmp_path: Path) -> None:
    target = tmp_path / "linked 工作区"
    _git(audit_repo, "worktree", "add", "-b", "audit-linked", str(target))

    entries = WorktreeManager(audit_repo).list_worktrees()

    assert target.resolve() in {entry.path.resolve() for entry in entries}
