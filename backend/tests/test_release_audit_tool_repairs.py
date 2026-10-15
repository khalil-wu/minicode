"""Permission and traversal regressions for the 2026-10-11 release review."""
from __future__ import annotations

import asyncio
import os
import subprocess
from contextlib import contextmanager
from pathlib import Path

from backend.config import PermissionSettings
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.sandbox import SandboxPolicy
from backend.tools.git_tools import GitStatusTool
from backend.tools.list_files import ListFilesTool


def context_for(root: Path) -> ToolExecutionContext:
    return ToolExecutionContext(
        permission=PermissionContext(mode="bypass", workspace_root=root),
        workspace_root=root, permission_checker=PermissionChecker(PermissionSettings(), root),
        sandbox_policy=SandboxPolicy.bypass(),
    )


def test_git_status_hides_denied_names_on_both_sides_of_a_real_rename(tmp_path):
    def git(*args):
        return subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)
    git("init")
    git("config", "user.name", "Audit fixture")
    git("config", "user.email", "fixture@example.invalid")
    (tmp_path / ".env").write_text("SECRET=fixture")
    (tmp_path / "old name 中文.txt").write_text("normal")
    git("add", ".")
    git("commit", "-m", "isolated baseline")
    git("mv", ".env", "renamed-secret.txt")
    git("mv", "old name 中文.txt", "new name 中文.txt")
    (tmp_path / ".env.production").write_text("SECRET=fixture")
    result = asyncio.run(GitStatusTool(tmp_path).execute({}, context_for(tmp_path)))
    assert not result.is_error, result.content
    assert ".env" not in result.content
    assert "renamed-secret.txt" not in result.content
    assert "old name 中文.txt -> new name 中文.txt" in result.content


def test_recursive_listing_stops_before_opening_the_unrequested_subtree(tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_text("a")
    expensive = tmp_path / "z-tree"
    expensive.mkdir()
    (expensive / "child.txt").write_text("child")
    original = os.scandir
    @contextmanager
    def guarded(path):
        assert Path(path) != expensive, "limit must stop before traversing the tail"
        with original(path) as entries:
            yield iter(sorted(entries, key=lambda entry: entry.name))
    monkeypatch.setattr(os, "scandir", guarded)
    result = asyncio.run(ListFilesTool().execute({"path": ".", "recursive": True, "limit": 1}, context_for(tmp_path)))
    assert not result.is_error, result.content
    assert "a.txt" in result.content
    assert "child.txt" not in result.content
    assert "limit was reached" in result.content


def test_model_visible_listing_contract_has_the_executed_options():
    properties = ListFilesTool().model_schema().parameters["properties"]
    assert "recursive" in properties and "directory" in properties


def test_git_status_does_not_disclose_parent_repository_siblings(tmp_path):
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    selected = tmp_path / "subproject"
    selected.mkdir()
    (tmp_path / "sibling-private.txt").write_text("private")
    (selected / "owned.txt").write_text("owned")
    # Stage the actual paths so porcelain reports the owned file rather than
    # Git's default collapsed untracked-directory record (subproject/).
    subprocess.run(["git", "add", "sibling-private.txt", "subproject/owned.txt"], cwd=tmp_path, check=True, capture_output=True)
    result = asyncio.run(GitStatusTool(selected).execute({}, context_for(selected)))
    assert not result.is_error, result.content
    assert "owned.txt" in result.content
    assert "sibling-private.txt" not in result.content


def test_listing_applies_allowlist_even_when_the_configured_denylist_is_empty(tmp_path):
    (tmp_path / "allowed.txt").write_text("allowed")
    (tmp_path / "private.txt").write_text("private")
    context = ToolExecutionContext(
        permission=PermissionContext(mode="confirm", workspace_root=tmp_path),
        workspace_root=tmp_path,
        permission_checker=PermissionChecker(PermissionSettings(path_denylist=[], path_allowlist=["allowed.txt"]), tmp_path),
    )
    result = asyncio.run(ListFilesTool().execute({"path": "."}, context))
    assert not result.is_error, result.content
    assert "allowed.txt" in result.content
    assert "private.txt" not in result.content


def test_recursive_listing_does_not_open_a_directory_after_it_fills_the_limit(tmp_path, monkeypatch):
    branch = tmp_path / "branch"
    branch.mkdir()
    (branch / "unrequested.txt").write_text("unrequested")
    original = os.scandir
    def guarded(path):
        assert Path(path) != branch, "returning the directory itself must not open its descendants after the limit"
        return original(path)
    monkeypatch.setattr(os, "scandir", guarded)
    result = asyncio.run(ListFilesTool().execute({"path": ".", "recursive": True, "limit": 1}, context_for(tmp_path)))
    assert not result.is_error, result.content
    assert "branch/" in result.content
    assert "unrequested.txt" not in result.content
