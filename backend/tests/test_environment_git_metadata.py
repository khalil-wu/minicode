"""Source's fixed Git metadata reads on the original execution attachment."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import os
from pathlib import Path
import subprocess
import tempfile

import pytest
import pytest_asyncio

from backend.agent.environment_prompt import build_environment_git_status
from backend.agent.environment_selection import bind_tool_execution_environment, close_tool_execution_environment
from backend.agent.environment_selection import TurnEnvironmentRequest, TurnEnvironmentSelection
from backend.sandbox.policy import (FileSystemAccessMode, FileSystemPath, FileSystemSandboxEntry,
    FileSystemSandboxPolicy, FileSystemSpecialPath, PermissionProfile, SandboxEnforcement)
from backend.tests.test_executor_tool_admission import executor_host
from backend.workspace.executor_service import ExecutorWorkspaceService


pytestmark = pytest.mark.parametrize("executor_host", ["local"], indirect=True)


@pytest_asyncio.fixture
async def git_metadata_context(executor_host):
    host = executor_host
    # The developer's home may itself be a Git repository. An independent
    # owned directory keeps the non-repository oracle free of ancestor repos.
    temporary_root = Path(os.environ["PUBLIC"]) if os.name == "nt" else Path(tempfile.gettempdir())
    with tempfile.TemporaryDirectory(prefix="minicode-git-metadata-", dir=temporary_root) as directory:
        root = Path(directory).resolve()
        assert root.parent == temporary_root.resolve() and root.name.startswith("minicode-git-metadata-")
        identity = host.environments.primary().selection.environment_id
        await host.owner.update_selections((TurnEnvironmentSelection.from_request(
            TurnEnvironmentRequest(identity, root.as_uri(), (root.as_uri(),))),))
        captured = await host.owner.snapshot()
        context = bind_tool_execution_environment(host.context, captured.primary())
        profile = PermissionProfile.managed(FileSystemSandboxPolicy.restricted((
            FileSystemSandboxEntry(FileSystemPath.special(FileSystemSpecialPath.ROOT), FileSystemAccessMode.READ),
            FileSystemSandboxEntry(FileSystemPath.special(FileSystemSpecialPath.PROJECT_ROOTS), FileSystemAccessMode.WRITE),
            FileSystemSandboxEntry(FileSystemPath.special(FileSystemSpecialPath.PROJECT_ROOTS, subpath=".git"), FileSystemAccessMode.READ),
        )))
        scope = replace(context.permission.environment_scope, permission_profile=profile)
        context = replace(context, workspace_root=root, permission=replace(context.permission,
            mode="confirm", sandbox_mode="workspace-write", environment_scope=scope))
        host.workspace = root
        try:
            yield host, context
        finally:
            await close_tool_execution_environment(context)
            await captured.aclose()


@pytest.mark.asyncio
async def test_confirm_nonrepository_metadata_does_not_require_a_command_sandbox_account(git_metadata_context, monkeypatch, record_testsuite_property):
    host, context = git_metadata_context
    def no_account(*_args, **_kwargs):
        raise AssertionError("A fixed full-read Git metadata query must not acquire a Windows sandbox account")
    monkeypatch.setattr("backend.sandbox.windows_native.discover_runtime", no_account)
    original_scope = context.permission.environment_scope
    record_testsuite_property("original_workspace", str(host.workspace))
    record_testsuite_property("original_scope_cwd", original_scope.cwd_uri)
    probe = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=host.workspace, capture_output=True)
    assert probe.returncode != 0
    record_testsuite_property("direct_git_root", probe.stdout.decode("utf-8", errors="replace"))
    snapshot = await build_environment_git_status(context)
    record_testsuite_property("actual_snapshot", snapshot)
    assert snapshot == ""
    assert context.permission.mode == "confirm"
    assert context.permission.environment_scope is original_scope
    assert original_scope.permission_profile.enforcement is SandboxEnforcement.MANAGED


@pytest.mark.asyncio
async def test_actual_repository_metadata_disables_repository_fsmonitor_and_hooks(git_metadata_context, monkeypatch):
    host, context = git_metadata_context
    root = host.workspace
    def git(*args):
        return subprocess.run(["git", *args], cwd=root, capture_output=True, check=True, timeout=10)
    await asyncio.to_thread(git, "init")
    (root / "untracked.txt").write_text("actual selected executor\n", encoding="utf-8")
    monitor = root / "fsmonitor-hook"
    monitor.write_text("#!/bin/sh\nprintf executed > fsmonitor-ran\nprintf 'token\\000/\\000'\n", encoding="utf-8", newline="\n")
    monitor.chmod(0o755)
    hook = root / ".git/hooks/post-index-change"
    hook.write_text("#!/bin/sh\nprintf executed > hooks-ran\n", encoding="utf-8", newline="\n")
    hook.chmod(0o755)
    await asyncio.to_thread(git, "config", "core.fsmonitor", "./fsmonitor-hook")
    await asyncio.to_thread(git, "status", "--short")
    assert (root / "fsmonitor-ran").read_text() == "executed"
    (root / "fsmonitor-ran").unlink()
    if (root / "hooks-ran").exists():
        (root / "hooks-ran").unlink()
    starts = []
    original_start = context.turn_environment.environment.process.start
    async def observed_start(argv, cwd, **kwargs):
        starts.append((argv, cwd, kwargs["sandbox"]))
        return await original_start(argv, cwd, **kwargs)
    monkeypatch.setattr(context.turn_environment.environment.process, "start", observed_start)
    original_scope = context.permission.environment_scope
    snapshot = await build_environment_git_status(context)
    assert "untracked.txt" in snapshot
    assert len(starts) == 6
    null = "NUL" if original_scope.platform_os == "windows" else "/dev/null"
    for argv, cwd, sandbox in starts:
        assert argv[:8] == ["git", "--no-optional-locks", "-c", "safe.bareRepository=explicit", "-c", f"core.hooksPath={null}", "-c", "core.fsmonitor=false"]
        assert cwd == original_scope.cwd_uri
        assert sandbox is None
    assert not (root / "fsmonitor-ran").exists() and not (root / "hooks-ran").exists()
    assert context.permission.environment_scope is original_scope
    assert context.permission.mode == "confirm"


@pytest.mark.asyncio
async def test_denied_read_metadata_retains_original_enforcing_scope(git_metadata_context, monkeypatch):
    host, context = git_metadata_context
    original = context.permission.environment_scope
    denied = FileSystemSandboxEntry(FileSystemPath("path", (host.workspace / "denied.txt").as_uri()), FileSystemAccessMode.DENY)
    scope = replace(original, permission_profile=replace(original.permission_profile,
        file_system=replace(original.permission_profile.file_system, entries=(*original.permission_profile.file_system.entries, denied))))
    context = replace(context, permission=replace(context.permission, environment_scope=scope))
    seen = []
    async def run(service, argv, **kwargs):
        assert kwargs["timeout"] == 5
        seen.append(kwargs["permission_scope"])
        return subprocess.CompletedProcess(argv, 128, b"", b"not a git repository")
    monkeypatch.setattr(ExecutorWorkspaceService, "run", run)
    assert await build_environment_git_status(context) == ""
    assert seen == [scope] and seen[0] is scope
    assert context.permission.environment_scope is scope
