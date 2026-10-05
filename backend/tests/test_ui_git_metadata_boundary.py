from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from backend import runtime_env
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.sandbox import SandboxPolicy, SandboxRunner
from backend.sandbox.runner import SandboxUnavailableError
from backend.services.health_service import build_git_doctor_payload
from backend.services.workspace_api_service import (
    run_ui_git_metadata,
    workspace_git_diff_payload,
    workspace_git_status_payload,
    workspace_git_worktree_payload,
)
from backend.tools.git_tools import GitStatusTool


def test_trusted_ui_git_metadata_works_without_model_sandbox_or_vault_while_tools_still_require_it(tmp_path, monkeypatch):
    root = tmp_path / "selected"
    root.mkdir()
    env = runtime_env.sanitized_git_env(root)
    for args in (
        ("init", "-q", "-b", "main"),
        ("config", "user.name", "Fixture"),
        ("config", "user.email", "fixture@example.invalid"),
    ):
        subprocess.run(["git", *args], cwd=root, env=env, capture_output=True, check=True)
    (root / "tracked.txt").write_text("before\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=root, env=env, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=root, env=env, capture_output=True, check=True)
    (root / "tracked.txt").write_text("after\n", encoding="utf-8")
    monkeypatch.setenv("MINICODE_STATE_ROOT", "C:/Users/ago/AppData/Roaming/minicode-desktop")
    monkeypatch.setenv("TAVILY_API_KEY", "fixture-secret-must-not-be-inherited")
    sandbox_calls = []

    async def unavailable(_runner, *args, **kwargs):
        sandbox_calls.append((args, kwargs))
        raise SandboxUnavailableError("model sandbox unavailable")

    def unresolved_vault(_scope):
        pytest.fail("UI Git metadata must not resolve model tool vault entries")

    monkeypatch.setattr(SandboxRunner, "spawn_interactive", unavailable)
    monkeypatch.setattr(runtime_env, "vault_subprocess_env", unresolved_vault)
    status = workspace_git_status_payload(root, workspace_root=root)
    worktrees = workspace_git_worktree_payload(root, workspace_root=root)
    diff = workspace_git_diff_payload(root, "tracked.txt", workspace_root=root)
    doctor = build_git_doctor_payload(root)
    assert status == {"is_git_repo": True, "branch": "main", "modified": ["tracked.txt"], "staged": [], "untracked": []}
    assert worktrees["is_git_repo"] is True and worktrees["current_branch"] == "main"
    assert worktrees["worktree_count"] == 1 and worktrees["current_path"] == str(root)
    assert Path(worktrees["common_git_dir"]) == root / ".git"
    assert diff["is_git_repo"] is True and "-before" in diff["diff"] and "+after" in diff["diff"]
    assert doctor["available"] is True and doctor["branch"] == "main" and doctor["changed"] == 1
    assert sandbox_calls == []

    context = ToolExecutionContext(
        permission=PermissionContext(mode="confirm", workspace_root=root), workspace_root=root,
        sandbox_policy=SandboxPolicy(workspace_root=root, fail_if_unavailable=True),
    )
    with pytest.raises(SandboxUnavailableError, match="model sandbox unavailable"):
        asyncio.run(GitStatusTool(root).execute({}, context=context))
    assert len(sandbox_calls) == 1


def test_ui_metadata_uses_fixed_git_flags_and_scrubbed_host_environment(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setenv("TAVILY_API_KEY", "fixture-secret")
    monkeypatch.setenv("GIT_DIR", "wrong-repository")

    def capture(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "main\n", "")

    monkeypatch.setattr("backend.services.workspace_api_service.subprocess.run", capture)
    result = run_ui_git_metadata(tmp_path, "branch", "--show-current")
    argv, options = calls[0]
    assert argv == ["git", "--no-optional-locks", "--literal-pathspecs", "branch", "--show-current"]
    assert options["cwd"] == tmp_path and options["stdin"] == subprocess.DEVNULL
    assert "TAVILY_API_KEY" not in options["env"] and "GIT_DIR" not in options["env"]
    assert result.stdout == "main\n"
