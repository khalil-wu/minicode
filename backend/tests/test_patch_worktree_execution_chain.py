from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.agent.run_context import RunContext
from backend.hooks.manager import HookEvent, HookResult
from backend.runtime_env import ShellEnvironmentPolicy, sanitized_git_env
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.sandbox import SandboxPolicy, SandboxRunner
from backend.tools import git_support, worktree_tools
from backend.tools.apply_patch import ApplyPatchTool
from backend.tools.file_tools_common import content_hash
from backend.tools.git_tools import GitDiffTool, GitLogTool, GitStatusTool
from backend.tools.registry import ToolRegistry
from backend.tools.worktree_tools import (
    CreateWorktreeTool, ListWorktreesTool, RemoveWorktreeTool,
    RestoreWorktreeTool, SnapshotWorktreeTool,
)
from backend.workspace import worktree


@pytest.mark.parametrize("body", [
    "*** Move to: \n@@\n-old\n+new",
    "@@\n*** End of File",
    "@@\n@@\n-old\n+new",
    "@@\n-old\n+new\n*** End of File\n+extra",
    "@@unspaced\n-old\n+new",
])
def test_patch_invalid_structure_fails_before_file_changes(tmp_path, body):
    source = tmp_path / "source.txt"
    source.write_text("unspaced\nold\n", encoding="utf-8")
    context = ToolExecutionContext(permission=PermissionContext(mode="bypass"), workspace_root=tmp_path,
        metadata={"_read_file_hashes": {str(source.resolve()): content_hash(source.read_text(encoding="utf-8"))}})
    registry = ToolRegistry()
    registry.register(ApplyPatchTool())
    result = asyncio.run(registry.execute("apply_patch", {"patch": "*** Begin Patch\n*** Update File: source.txt\n" + body + "\n*** End Patch"}, context=context))
    assert result.is_error
    assert source.read_text(encoding="utf-8") == "unspaced\nold\n"


@pytest.mark.parametrize("tool,args", [(GitDiffTool, {"context_lines": -1}), (GitLogTool, {"limit": -1})])
def test_git_numeric_options_reject_before_command_execution(tmp_path, tool, args, monkeypatch):
    async def forbidden(*a, **kw):
        pytest.fail("invalid Git options reached command execution")
    monkeypatch.setattr("backend.tools.git_tools._run_git", forbidden)
    registry = ToolRegistry()
    registered = tool(tmp_path)
    registry.register(registered)
    result = asyncio.run(registry.execute(registered.name, args))
    assert result.error_kind == "validation_error"


def test_git_programming_failure_reaches_the_shared_typed_boundary(tmp_path, monkeypatch):
    async def broken(*args, **kwargs):
        raise RuntimeError("record-only failure")
    monkeypatch.setattr("backend.tools.git_tools._run_git", broken)
    registry = ToolRegistry()
    registry.register(GitStatusTool(tmp_path))
    result = asyncio.run(registry.execute("git_status", {}))
    assert result.is_error and result.status == "failed"
    assert "RuntimeError: record-only failure" in result.content


def test_projectless_worktree_tools_never_consult_the_global_manager(monkeypatch):
    monkeypatch.setattr(worktree, "get_global_worktree_manager", lambda: pytest.fail("projectless borrowed global repository"))
    result = asyncio.run(ListWorktreesTool().execute({}, ToolExecutionContext(permission=PermissionContext(), workspace_root=None)))
    assert result.is_error


def test_relative_hook_worktree_removal_uses_its_context_root(tmp_path, monkeypatch):
    paths = []
    class Hooks:
        def has_hooks(self, event):
            return event in {HookEvent.WORKTREE_CREATE, HookEvent.WORKTREE_REMOVE}
        async def run_worktree_create(self, **kwargs):
            return HookResult(worktree_path="hook-child")
        async def run_worktree_remove(self, **kwargs):
            paths.append(kwargs["path"])
            return HookResult()
    monkeypatch.setattr(worktree_tools, "_HOOK_CREATED_WORKTREES", set())
    context = ToolExecutionContext(permission=PermissionContext(), workspace_root=tmp_path, run_context=RunContext(hook_manager=Hooks()))
    async def run():
        assert not (await CreateWorktreeTool().execute({"path": "requested"}, context)).is_error
        assert not (await RemoveWorktreeTool().execute({"path": "hook-child"}, context)).is_error
    asyncio.run(run())
    assert paths == [str((tmp_path / "hook-child").resolve())]


@pytest.fixture
def recorded_git(tmp_path, monkeypatch):
    repository = tmp_path / "repo"
    (repository / ".git").mkdir(parents=True)
    captures = []
    class Runner:
        def __init__(self, policy, *, env_filter=None):
            self.policy = policy
        def capability(self, **kwargs):
            return SimpleNamespace(backend="record-only")
        def map_path_to_sandbox(self, path):
            return str(Path(path).resolve())
        def map_path_from_sandbox(self, path):
            return path
        async def spawn_interactive(self, argv, **kwargs):
            captures.append({"argv": argv, "cwd": Path(kwargs["cwd"]), "policy": self.policy})
            return SimpleNamespace(argv=argv, returncode=0)
        async def cleanup(self):
            return True
    async def communicate(process, **kwargs):
        argv = process.argv
        if "rev-parse" in argv:
            output = str(repository / ".git") + "\n" + str(repository / ".git") if "--git-common-dir" in argv else "a" * 40
        elif "write-tree" in argv:
            output = "b" * 40
        elif "commit-tree" in argv:
            output = "c" * 40
        else:
            output = ""
        return output.encode(), b""
    monkeypatch.setattr("backend.sandbox.SandboxRunner", Runner)
    monkeypatch.setattr(git_support, "SandboxRunner", Runner)
    monkeypatch.setattr(git_support, "communicate_bounded", communicate)
    monkeypatch.setattr(worktree.subprocess, "run", lambda *a, **kw: pytest.fail("model-facing WorktreeManager used host subprocess"))
    return repository, captures, Runner


def test_worktree_execution_retains_captured_policy_and_exact_target_grants(recorded_git):
    repository, captures, _ = recorded_git
    policy = SandboxPolicy(workspace_root=repository, writable_roots=(repository,), allow_network=False)
    context = ToolExecutionContext(permission=PermissionContext(), workspace_root=repository, sandbox_policy=policy)
    result = asyncio.run(CreateWorktreeTool().execute({"path": "child", "branch": "codex/audit", "new_branch": True}, context))
    assert not result.is_error
    operation = next(item for item in captures if "add" in item["argv"])
    target = repository / ".minicode" / "worktrees" / "child"
    assert str(target) in operation["argv"]
    assert all(not item["policy"].allow_network for item in captures)
    resolved = operation["policy"].resolve(cwd=repository)
    assert any(root.is_path_writable(target) for root in resolved.writable_roots)
    assert any(root.is_path_writable(repository / ".git") for root in resolved.writable_roots)
    assert not any(root.is_path_writable(repository / ".minicode" / "rules") for root in resolved.writable_roots)
    assert context.sandbox_policy is policy


def test_readonly_worktree_policy_does_not_gain_host_or_metadata_write(recorded_git):
    repository, captures, _ = recorded_git
    policy = SandboxPolicy(workspace_root=repository, readable_roots=(repository,), allow_network=False)
    context = ToolExecutionContext(permission=PermissionContext(), workspace_root=repository, sandbox_policy=policy)
    registry = ToolRegistry()
    registry.register(CreateWorktreeTool())
    result = asyncio.run(registry.execute("create_worktree", {"path": "child"}, context=context))
    assert result.is_error and "workspace-write" in result.content
    assert not any("add" in item["argv"] for item in captures)


def test_native_linked_worktree_status_keeps_captured_repository_owner(recorded_git, monkeypatch):
    repository, captures, runner_type = recorded_git
    child = repository / ".minicode" / "worktrees" / "linked"
    child.mkdir(parents=True)
    policy = SandboxPolicy(workspace_root=repository, readable_roots=(repository,), allow_network=False)
    context = ToolExecutionContext(permission=PermissionContext(), workspace_root=repository, sandbox_policy=policy)
    monkeypatch.setattr(runner_type, "capability", lambda self, **kw: SimpleNamespace(backend="windows-elevated-wfp"))

    async def run():
        manager = await worktree_tools._resolve_worktree_manager(context)
        return await asyncio.to_thread(manager.has_local_changes, child)

    assert not asyncio.run(run())
    operation = next(item for item in captures if "status" in item["argv"])
    assert operation["cwd"] == repository
    assert operation["argv"][operation["argv"].index("-C") + 1] == str(child)
    effective = operation["policy"]
    assert effective.workspace_root == policy.workspace_root and effective.workspace_roots == policy.workspace_roots
    assert effective.permission_profile == policy.permission_profile
    assert effective.allow_network == policy.allow_network
    assert effective.timeout == worktree.WORKTREE_GIT_TIMEOUT_SECONDS
    assert effective.resolve(cwd=repository).resolved_entries == policy.resolve(cwd=repository).resolved_entries
    assert not any(root.is_path_writable(repository / ".git") for root in effective.resolve(cwd=repository).writable_roots)


@pytest.mark.parametrize("failure", [ValueError("invalid configuration"), PermissionError("denied discovery"), FileNotFoundError("Git unavailable"), subprocess.TimeoutExpired("git", 1)])
def test_worktree_discovery_failure_does_not_become_not_a_repository(recorded_git, monkeypatch, failure):
    repository, _, runner_type = recorded_git
    async def fail(*args, **kwargs):
        raise failure
    monkeypatch.setattr(runner_type, "spawn_interactive", fail)
    context = ToolExecutionContext(permission=PermissionContext(), workspace_root=repository,
        sandbox_policy=SandboxPolicy(workspace_root=repository, readable_roots=(repository,)))
    registry = ToolRegistry()
    registry.register(ListWorktreesTool())
    result = asyncio.run(registry.execute("list_worktrees", {}, context=context))
    assert result.is_error and result.status == "failed"
    assert type(failure).__name__ in result.content


def test_only_genuine_git_not_repository_becomes_the_not_repository_result(recorded_git, monkeypatch):
    repository, _, _ = recorded_git
    async def not_repository(process, **kwargs):
        process.returncode = 128
        return b"", b"fatal: not a git repository (or any of the parent directories): .git"
    monkeypatch.setattr(git_support, "communicate_bounded", not_repository)
    result = asyncio.run(ListWorktreesTool().execute({}, ToolExecutionContext(permission=PermissionContext(), workspace_root=repository,
        sandbox_policy=SandboxPolicy(workspace_root=repository, readable_roots=(repository,)))))
    assert result.is_error and "不是 Git 仓库" in result.content


def test_snapshot_relative_path_and_private_index_retain_current_owner(recorded_git):
    repository, captures, _ = recorded_git
    child = repository / ".minicode" / "worktrees" / "child"
    child.mkdir(parents=True)
    policy = SandboxPolicy(workspace_root=repository, writable_roots=(repository,), allow_network=False)
    context = ToolExecutionContext(permission=PermissionContext(), workspace_root=repository, sandbox_policy=policy, conversation_id="record-snapshot")
    result = asyncio.run(SnapshotWorktreeTool().execute({"path": ".minicode/worktrees/child"}, context))
    assert not result.is_error
    snapshot_index_calls = [item for item in captures if "GIT_INDEX_FILE" in item["policy"].env_overrides]
    assert snapshot_index_calls
    index_operations = [item for item in snapshot_index_calls if any(command in item["argv"] for command in ("read-tree", "add", "write-tree"))]
    assert all(item["cwd"] == child for item in index_operations)
    assert len({item["policy"].env_overrides["GIT_INDEX_FILE"] for item in snapshot_index_calls}) == 1
    assert context.sandbox_policy.env_overrides == {}


def test_worktree_cancellation_keeps_worker_until_git_cleanup_finishes(recorded_git, monkeypatch):
    repository, _, runner_type = recorded_git
    original_communicate = git_support.communicate_bounded
    original_spawn = runner_type.spawn_interactive
    async def run():
        started, cleanup_release = asyncio.Event(), asyncio.Event()
        async def spawn(self, argv, **kwargs):
            self.held = "worktree" in argv and "add" in argv
            return await original_spawn(self, argv, **kwargs)
        async def communicate(process, **kwargs):
            if "worktree" in process.argv and "add" in process.argv:
                started.set()
                await asyncio.Event().wait()
            return await original_communicate(process, **kwargs)
        async def cleanup(self):
            if getattr(self, "held", False):
                await cleanup_release.wait()
            return True
        monkeypatch.setattr(runner_type, "spawn_interactive", spawn)
        monkeypatch.setattr(runner_type, "cleanup", cleanup)
        monkeypatch.setattr(git_support, "communicate_bounded", communicate)
        context = ToolExecutionContext(permission=PermissionContext(), workspace_root=repository,
            sandbox_policy=SandboxPolicy(workspace_root=repository, writable_roots=(repository,)), cancel_event=asyncio.Event())
        registry = ToolRegistry()
        registry.register(CreateWorktreeTool())
        task = asyncio.create_task(registry.execute("create_worktree", {"path": "child"}, context=context))
        try:
            await asyncio.wait_for(started.wait(), 1)
            context.cancel_event.set()
            await asyncio.sleep(.02)
            assert not task.done()
        finally:
            cleanup_release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(run())


def test_public_worktree_create_snapshot_restore_in_a_private_repository(tmp_path, monkeypatch):
    repository = tmp_path / "repo"
    repository.mkdir()
    def git(*args):
        return subprocess.run(["git", *args], cwd=repository, capture_output=True, check=True)
    git("init")
    for name, value in (("user.name", "Audit"), ("user.email", "audit@example.invalid"), ("commit.gpgsign", "false"), ("core.hooksPath", "NUL")):
        git("config", name, value)
    (repository / "README.txt").write_text("original", encoding="utf-8")
    git("add", "README.txt")
    git("commit", "-m", "fixture")
    captures = []
    class ObservedRunner(SandboxRunner):
        async def spawn_interactive(self, argv, **kwargs):
            captures.append({"argv": argv, "cwd": str(kwargs["cwd"]), "network": self._policy.allow_network,
                "enforcement": self._policy.resolve().enforcement.value, "index": self._policy.env_overrides.get("GIT_INDEX_FILE", "")})
            return await super().spawn_interactive(argv, **kwargs)
    monkeypatch.setattr(git_support, "SandboxRunner", ObservedRunner)
    monkeypatch.setattr("backend.sandbox.SandboxRunner", ObservedRunner)
    context = ToolExecutionContext(permission=PermissionContext(mode="bypass", sandbox_mode="danger-full-access", approval_policy="never"), workspace_root=repository,
        sandbox_policy=SandboxPolicy(workspace_root=repository, disable_os_sandbox=True, allow_network=False), conversation_id="private-worktree")
    registry = ToolRegistry()
    for tool in (CreateWorktreeTool(), SnapshotWorktreeTool(), RestoreWorktreeTool()):
        registry.register(tool)
    async def run():
        created = await registry.execute("create_worktree", {"path": "child", "branch": "codex/audit-child", "new_branch": True}, context=context)
        assert not created.is_error, created.content
        child = repository / ".minicode" / "worktrees" / "child"
        (child / "README.txt").write_text("changed", encoding="utf-8")
        (child / "note.txt").write_text("untracked", encoding="utf-8")
        snapshot = await registry.execute("worktree_snapshot", {"path": ".minicode/worktrees/child"}, context=context)
        assert not snapshot.is_error, snapshot.content
        manager = await worktree_tools._resolve_worktree_manager(context)
        record = (await asyncio.to_thread(manager.list_snapshots, "private-worktree"))[0]
        restored = await registry.execute("worktree_restore", {"snapshot_id": record.id, "dest": ".minicode/worktrees/restored"}, context=context)
        assert not restored.is_error, restored.content
        destination = repository / ".minicode" / "worktrees" / "restored"
        assert (destination / "README.txt").read_text(encoding="utf-8") == "changed"
        assert (destination / "note.txt").read_text(encoding="utf-8") == "untracked"
        (Path(__import__("os").environ["MINICODE_STATE_ROOT"]).parent / "real-worktree-chain.json").write_text(json.dumps({"created": str(child), "restored": str(destination), "snapshot_id": record.id, "captures": captures, "recursive_removals": 0}, indent=2), encoding="utf-8")
    asyncio.run(run())


@pytest.mark.parametrize("source", ["inherited", "set_values", "env_overrides"])
@pytest.mark.parametrize("mixed_case", [False, True])
def test_git_environment_selectors_cannot_replace_the_selected_private_repository(tmp_path, monkeypatch, source, mixed_case):
    if mixed_case and os.name != "nt":
        pytest.skip("Windows environment variable case semantics")
    selected, foreign = tmp_path / "selected", tmp_path / "foreign"
    for directory in (selected, foreign):
        directory.mkdir()
        subprocess.run(["git", "init"], cwd=directory, env=sanitized_git_env(), capture_output=True, check=True)
        subprocess.run(["git", "config", "core.fsmonitor", "false"], cwd=directory, env=sanitized_git_env(), capture_output=True, check=True)
    (selected / "selected.txt").write_text("selected", encoding="utf-8")
    (foreign / "foreign.txt").write_text("foreign", encoding="utf-8")
    values = {("gIt_DiR" if mixed_case else "GIT_DIR"): str(foreign / ".git"),
        ("gIt_WoRk_TrEe" if mixed_case else "GIT_WORK_TREE"): str(foreign)}
    shell = ShellEnvironmentPolicy(inherit="all" if source == "inherited" else "core",
        set_values=values if source == "set_values" else {})
    if source == "inherited":
        for key, value in values.items():
            monkeypatch.setenv(key, value)
    policy = SandboxPolicy(workspace_root=selected, disable_os_sandbox=True, shell_environment_policy=shell,
        env_overrides=values if source == "env_overrides" else {})
    context = ToolExecutionContext(permission=PermissionContext(mode="bypass"), workspace_root=selected, sandbox_policy=policy)
    registry = ToolRegistry()
    registry.register(GitStatusTool(selected))
    async def run():
        # A controlled filter-off sample reproduces the former env-routing
        # behavior on two generated repositories; it never writes either repo.
        with monkeypatch.context() as prior:
            prior.setattr(git_support, "_is_git_repository_env", lambda name: False)
            before = await registry.execute("git_status", {}, context=context)
        after = await registry.execute("git_status", {}, context=context)
        assert "foreign.txt" in before.content and "selected.txt" not in before.content
        assert "selected.txt" in after.content and "foreign.txt" not in after.content
        assert context.sandbox_policy is policy
        output = Path(os.environ["MINICODE_STATE_ROOT"]).parent / f"selector-{source}-{'mixed' if mixed_case else 'upper'}.json"
        output.write_text(json.dumps({"source": source, "mixed_case": mixed_case, "control_filter_off": before.content, "after": after.content,
            "selected_repository": str(selected), "foreign_repository": str(foreign), "writes_by_tool": 0}, indent=2), encoding="utf-8")
    asyncio.run(run())


def test_git_discovery_does_not_borrow_a_repository_from_the_host_home(tmp_path, monkeypatch):
    home = tmp_path / "host-home"
    home.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=home, env=sanitized_git_env(), check=True)
    workspace = home / "ordinary-folder"
    workspace.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path / "unrelated-selector"))
    policy = SandboxPolicy(workspace_root=workspace, disable_os_sandbox=True)
    result = asyncio.run(git_support._run_git(["git", "rev-parse", "--show-toplevel"], root=workspace, sandbox_policy=policy))
    assert result.returncode != 0
    assert b"not a git repository" in result.stderr.lower()
