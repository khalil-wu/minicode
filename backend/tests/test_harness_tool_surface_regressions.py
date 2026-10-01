"""Regressions reproduced by the fresh gpt-6-luna harness audit."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from backend.config import AgentSettings, AppConfig, LLMSettings, PermissionSettings
from backend.artifact.store import ArtifactStore
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext
from backend.permissions.context import ToolExecutionContext
from backend.tests.test_code_execution import FixtureTool, ScriptModel, run_model
from backend.tools.agent_tools import TaskTool
from backend.tools.base import ToolResult, ToolSchema
from backend.tools.code_execution import ToolExecTool, ToolWaitTool
from backend.tools.code_execution import _present_result
from backend.tools.contracts import ToolSpec
from backend.tools.registry import ToolRegistry
from backend.tools.schema import code_mode_parameters
from backend.tools.search_tools import GrepFilesTool
from backend.tools.subagent_context import build_agent_execution_profile, subagent_toolset_policy
from backend.tools.toolsets import ACTIVE_TOOLSET_POLICY_METADATA_KEY, SESSION_TOOLSET_POLICY_METADATA_KEY, ToolsetPolicy
from backend.ws.handler import WebSocketSession


@pytest.mark.parametrize("body", ["source line\n" * 3000, '\\"\t\n' * 4000], ids=["multiline", "json-escaping"])
def test_large_code_output_preserves_content_and_exact_receipt(tmp_path, body):
    async def scenario():
        output = json.dumps("SOURCE_BEGIN\n" + body + "\nSOURCE_END")
        model = ScriptModel(f"text({output});", max_chars=512)
        state, _, _, events, _ = await run_model(tmp_path, model)
        assert state.terminal_status == "completed"
        receipt = model.reports[-1]
        assert "SOURCE_BEGIN" in receipt["output_preview"]
        assert "SOURCE_END" in receipt["output_preview"]
        assert "First line exceeds" not in receipt["output_preview"]
        result = next(event for event in events if event.type == "tool_result" and event.data["id"] == "script-parent")
        assert len(result.data["summary"]) <= 512
        assert receipt["artifact_id"].startswith("art_")
        assert receipt["cell_id"].startswith("cell_")
    asyncio.run(scenario())


def test_code_mode_directory_refreshes_when_nested_tools_change():
    registry = ToolRegistry()
    registry.register(ToolExecTool())
    registry.register(ToolWaitTool())
    policy = ToolsetPolicy(code_mode_only=True)

    def description():
        schemas = registry.get_schemas(toolset_policy=policy)
        return next(item["function"]["description"] for item in schemas if item["function"]["name"] == "tool_exec")

    assert "rows(" not in description()
    registry.register(FixtureTool())
    assert "rows(" in description()
    registry.unregister("rows")
    assert "rows(" not in description()
    assert "tool_exec" not in {item["function"]["name"] for item in registry.get_schemas(toolset_policy=ToolsetPolicy(code_mode_enabled=False))}
    assert "tool_wait" not in {item["function"]["name"] for item in registry.get_schemas(toolset_policy=ToolsetPolicy(code_mode_enabled=False))}


def test_unavailable_cell_keeps_recovery_receipt_when_outcomes_are_large(tmp_path):
    async def scenario():
        store = ArtifactStore(storage_dir=tmp_path / "artifacts")
        report = {"cell_id": "lost-cell", "status": "unavailable", "error": "Inspect recorded outcomes before retrying writes.", "completed_tools": [{"output": "x" * 10000}]}
        result = await _present_result(ToolResult(json.dumps(report), is_error=True, status="failed", runtime_metadata={"code_cell": report}),
                                       ToolExecutionContext(permission=PermissionContext(), artifact_store=store), 512)
        preview = json.loads(result.content)
        assert preview["cell_id"] == "lost-cell"
        assert preview["status"] == "unavailable"
        assert "Inspect recorded outcomes" in preview["output_preview"]
        assert result.is_error and result.artifact_id
        store.shutdown()
    asyncio.run(scenario())


def test_nested_directory_preserves_task_object_and_alternative_required_fields():
    signature = code_mode_parameters(TaskTool.get_schema(TaskTool.__new__(TaskTool)).parameters)
    assert "description: string" in signature
    assert "prompt: string" in signature
    assert "parallel_tasks: Array<{" in signature
    assert '"general-purpose" | "explore" | "plan"' in signature
    assert "write_scope?: Array<string>" in signature
    assert "run_in_background?: boolean" in signature


def test_code_mode_directory_never_emits_invalid_javascript_property_names():
    signature = code_mode_parameters(GrepFilesTool().model_schema().parameters)
    assert '"-n"' not in signature
    assert "line_numbers?: boolean" in signature
    assert "before_context?: number" in signature
    legacy = code_mode_parameters(GrepFilesTool().get_schema().parameters)
    assert '"-n"?: boolean' in legacy


def test_nested_delegation_inherits_session_ceiling_not_code_cell_routing(tmp_path):
    class InspectParent(FixtureTool):
        async def execute(self, args, context=None):
            self.session_policy = context.metadata[SESSION_TOOLSET_POLICY_METADATA_KEY]
            self.cell_policy = context.metadata[ACTIVE_TOOLSET_POLICY_METADATA_KEY]
            return ToolResult("captured")

    async def scenario():
        tool = InspectParent("inspect_parent")
        model = ScriptModel('text((await tools.inspect_parent({})).content);')
        state, _, _, _, _ = await run_model(tmp_path, model, [tool], code_mode_only=True)
        assert state.terminal_status == "completed", model.reports
        assert tool.session_policy.is_available(ToolSpec(name="tool_exec"))
        assert not tool.cell_policy.is_available(ToolSpec(name="tool_exec"))
        assert tool.session_policy.availability_filters == ()
    asyncio.run(scenario())


def test_readonly_delegations_and_parent_reads_share_reader_admission(tmp_path):
    class ParallelDelegate(TaskTool):
        async def execute(self, args, context=None):
            self.entered += 1
            if self.entered == 2:
                self.ready.set()
            await self.ready.wait()
            return ToolResult(args["description"])

    async def scenario():
        task = ParallelDelegate(artifact_store=ArtifactStore(storage_dir=tmp_path / "delegate-artifacts"))
        task.entered, task.ready = 0, asyncio.Event()
        model = ScriptModel('const results = await Promise.all([tools.task({description:"left",prompt:"read",agent_type:"explore"}), tools.task({description:"right",prompt:"read",read_only:true}), tools.read_file({})]); text(results.map(result => result.content));')
        state, _, _, _, _ = await run_model(tmp_path, model, [task, FixtureTool("read_file", count=0)], code_mode_only=True)
        assert state.terminal_status == "completed", model.reports
        assert task.entered == 2
        assert "left" in model.reports[-1]["output"][0]
        task._artifact_store.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize("profile", [build_agent_execution_profile(background=True), build_agent_execution_profile(team_mode=True)])
def test_background_and_teammate_code_mode_keep_leaf_permissions(profile, tmp_path):
    registry = ToolRegistry()
    for tool in (ToolExecTool(), ToolWaitTool(), FixtureTool("read_file", count=1), FixtureTool("write_file", effect=True)):
        registry.register(tool)
    policy = replace(subagent_toolset_policy(execution_profile=profile), code_mode_only=True)
    checker = PermissionChecker(PermissionSettings(), tmp_path)
    permission = PermissionContext(mode="plan")
    schemas = registry.get_schemas(toolset_policy=policy, permission_checker=checker, permission_context=permission)
    assert {"tool_exec", "tool_wait"} <= {item["function"]["name"] for item in schemas}
    directory = next(item["function"]["description"] for item in schemas if item["function"]["name"] == "tool_exec")
    assert "read_file(" in directory
    assert "write_file(" not in directory


def test_capability_snapshot_materializes_direct_schema_once():
    class Counted(FixtureTool):
        schema_reads = 0

        def get_spec(self):
            return ToolSpec(name=self.name)

        def get_schema(self):
            self.schema_reads += 1
            return ToolSchema(self.name, "Rows", {"type": "object", "properties": {}})

    registry = ToolRegistry()
    tool = Counted()
    registry.register(tool)
    snapshot = registry.build_snapshot()
    assert snapshot["summary"]["direct_tools"] == 1
    assert tool.schema_reads == 1
    registry.build_snapshot()
    assert tool.schema_reads == 1


def test_runtime_projection_uses_admitted_policy_and_hides_unbound_workspace_tools():
    session = WebSocketSession.__new__(WebSocketSession)
    session.conversation_runtime = SimpleNamespace(active_conversation_id=None)
    session.active_conversation_id = None
    session.run_manager = SimpleNamespace(context_for=lambda _: None)
    session._extension_runtime_states = {}
    session.config = AppConfig(llm=LLMSettings(api_key="fixture"), agent=AgentSettings(code_mode_only=True))
    session.permission_context = PermissionContext(mode="confirm")
    session.session_lifecycle = SimpleNamespace(current_workspace_root=lambda: None)
    session.llm = SimpleNamespace(configured_tool_mode=lambda: "")
    registry = ToolRegistry()
    registry.register(ToolExecTool())
    registry.register(FixtureTool("read_file"))
    idle = session.runtime_toolset_policy(registry)
    assert idle.code_mode_only
    assert not idle.is_available(registry.get_tool_spec("read_file"))
    admitted = ToolsetPolicy(disabled_tools=frozenset({"read_file"}))
    session.run_manager = SimpleNamespace(context_for=lambda _: SimpleNamespace(toolset_policy=admitted))
    assert session.runtime_toolset_policy(registry) is admitted


def _git_launch_review_sync(function):
    import asyncio
    import functools
    @functools.wraps(function)
    def execute(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))
    return execute


# Git launch-boundary review regressions. No repository-selected executable
# gets host authority merely because the typed operation is readonly.
@_git_launch_review_sync
async def test_git_launch_review_snapshot_and_transport_contract(monkeypatch, tmp_path):
    import subprocess as git_subprocess
    from backend.tools import git_support
    from backend.permissions.context import PermissionContext, ToolExecutionContext
    from backend.sandbox import SandboxPolicy

    snapshot = SandboxPolicy.workspace_default(tmp_path)
    context = ToolExecutionContext(PermissionContext(), workspace_root=tmp_path, sandbox_policy=snapshot)
    seen = {}
    process = type("GitProcess", (), {"returncode": 0})()

    class Runner:
        def __init__(self, policy):
            seen["policy"] = policy
        def capability(self, *, cwd=None):
            from types import SimpleNamespace
            return SimpleNamespace(backend="full-access")
        async def spawn_shell_interactive(self, command, **kwargs):
            seen["command"] = command
            seen["cwd"] = kwargs["cwd"]
            return process
        async def cleanup(self):
            seen["cleaned"] = True
            return True

    async def communicate(proc, **kwargs):
        assert proc is process
        assert kwargs["stdout_limit_bytes"] == 20 * 1024 * 1024
        assert kwargs["stderr_limit_bytes"] == 20 * 1024 * 1024
        return b" M tracked.py\0", b""

    monkeypatch.setattr(git_support, "SandboxRunner", Runner)
    monkeypatch.setattr(git_support, "communicate_bounded", communicate)
    result = await git_support._run_git(["git", "status", "--short"], root=tmp_path, context=context)
    assert isinstance(result, git_subprocess.CompletedProcess)
    assert result.stdout == b" M tracked.py\0"
    assert seen["policy"] is snapshot
    assert seen["cleaned"]
    assert "--no-optional-locks" in seen["command"]


@_git_launch_review_sync
async def test_git_launch_review_repeated_cancel_keeps_actual_cleanup_owned(monkeypatch, tmp_path):
    import asyncio
    import pytest
    from backend.tools import git_support
    from backend.permissions.context import PermissionContext, ToolExecutionContext
    from backend.sandbox import SandboxPolicy

    spawned = asyncio.Event()
    cleanup_started = asyncio.Event()
    release = asyncio.Event()
    context = ToolExecutionContext(PermissionContext(), workspace_root=tmp_path,
                                   sandbox_policy=SandboxPolicy.workspace_default(tmp_path))

    class Runner:
        def __init__(self, policy):
            pass
        def capability(self, *, cwd=None):
            from types import SimpleNamespace
            return SimpleNamespace(backend="full-access")
        async def spawn_shell_interactive(self, command, **kwargs):
            spawned.set()
            return object()
        async def cleanup(self):
            cleanup_started.set()
            return release.is_set()

    async def communicate(proc, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(git_support, "SandboxRunner", Runner)
    monkeypatch.setattr(git_support, "communicate_bounded", communicate)
    task = asyncio.create_task(git_support._run_git(["git", "status"], root=tmp_path, context=context))
    await asyncio.wait_for(spawned.wait(), 1)
    task.cancel()
    await asyncio.wait_for(cleanup_started.wait(), 1)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done(), "a receipt must not retire the runner's real owner"
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)


@_git_launch_review_sync
async def test_git_launch_review_overflow_releases_runner(monkeypatch, tmp_path):
    import pytest
    from backend.tools import git_support
    from backend.subprocesses import SubprocessOutputLimitError
    from backend.sandbox import SandboxPolicy

    closed = []
    class Runner:
        def __init__(self, policy):
            pass
        def capability(self, *, cwd=None):
            from types import SimpleNamespace
            return SimpleNamespace(backend="full-access")
        async def spawn_shell_interactive(self, command, **kwargs):
            return object()
        async def cleanup(self):
            closed.append(True)
            return True

    async def communicate(proc, **kwargs):
        raise SubprocessOutputLimitError(stream_name="stdout", limit_bytes=20 * 1024 * 1024, captured=b"prefix")

    monkeypatch.setattr(git_support, "SandboxRunner", Runner)
    monkeypatch.setattr(git_support, "communicate_bounded", communicate)
    with pytest.raises(SubprocessOutputLimitError):
        await git_support._run_git(["git", "log"], root=tmp_path, sandbox_policy=SandboxPolicy.bypass())
    assert closed == [True]


@_git_launch_review_sync
async def test_git_launch_review_exact_metadata_grant_preserves_other_authority(monkeypatch, tmp_path):
    import subprocess
    import pytest
    from backend.tools import git_support
    from backend.sandbox import SandboxPolicy
    from backend.sandbox.policy import FileSystemAccessMode

    (tmp_path / ".git").mkdir()
    (tmp_path / ".minicode").mkdir()
    (tmp_path / "secret").mkdir()
    policy = SandboxPolicy(workspace_root=tmp_path, writable_roots=(tmp_path,),
                           denied_roots=(tmp_path / "secret",))
    calls = []
    async def discover(argv, **kwargs):
        calls.append(kwargs)
        return subprocess.CompletedProcess(argv, 0,
            (str(tmp_path / ".git") + "\n" + str(tmp_path / ".git") + "\n").encode(), b"")

    monkeypatch.setattr(git_support, "_run_git", discover)
    granted = await git_support._git_metadata_write_policy(policy, tmp_path, None)
    resolved = granted.resolve(cwd=tmp_path)
    assert resolved.resolve_access(tmp_path / ".git" / "index") is FileSystemAccessMode.WRITE
    assert resolved.resolve_access(tmp_path / ".minicode" / "rules.json") is FileSystemAccessMode.READ
    assert resolved.resolve_access(tmp_path / "secret" / "key") is FileSystemAccessMode.DENY
    assert not resolved.allow_network
    assert granted.protect_workspace_metadata
    assert calls[0]["sandbox_policy"] is policy
    with pytest.raises(PermissionError, match="workspace-write"):
        await git_support._git_metadata_write_policy(SandboxPolicy(workspace_root=tmp_path), tmp_path, None)

    # Use a non-TMPDIR metadata path: legacy policies intentionally grant temp
    # write access, so tmp_path.parent is not an ungranted authority oracle.
    external_metadata = type(tmp_path)(tmp_path.anchor) / "minicode-git-policy-review" / "other-git"
    async def external(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, str(external_metadata).encode() + b"\n", b"")
    monkeypatch.setattr(git_support, "_run_git", external)
    with pytest.raises(PermissionError, match="outside the workspace"):
        await git_support._git_metadata_write_policy(policy, tmp_path, None)
    # A linked repository's metadata already explicitly writable in the
    # captured policy needs no new privilege and must not be gratuitously denied.
    explicit = SandboxPolicy(workspace_root=tmp_path,
                             writable_roots=(tmp_path, external_metadata))
    assert await git_support._git_metadata_write_policy(explicit, tmp_path, None) is explicit


@_git_launch_review_sync
async def test_git_launch_review_real_fsmonitor_and_commit_boundary(tmp_path, monkeypatch):
    import json
    import subprocess
    from pathlib import Path
    from backend.runtime_env import sanitized_git_env
    from backend.permissions.context import PermissionContext, ToolExecutionContext
    from backend.sandbox import SandboxPolicy, SandboxRunner
    from backend.sandbox.runner import _container_readable_target
    from backend.tools import git_tools
    from backend.tools.git_support import _run_git

    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / ".minicode").mkdir()
    (root / ".minicode" / "rules.json").write_text("unchanged", encoding="utf-8")
    (root / "tracked.py").write_text("initial\n", encoding="utf-8")
    def host(*args):
        return subprocess.run(["git", *args], cwd=root, env=sanitized_git_env(),
                              capture_output=True, timeout=10, check=True)
    host("init")
    host("config", "user.name", "Git Boundary Review")
    host("config", "user.email", "git-boundary@example.invalid")
    host("add", "-A")
    host("commit", "-m", "fixture")
    readonly = SandboxPolicy(workspace_root=root, readable_roots=(outside,))
    write = SandboxPolicy(workspace_root=root, writable_roots=(root,), readable_roots=(outside,))
    capability = SandboxRunner(readonly).capability(cwd=root)
    assert capability.available and capability.filesystem_isolated, capability.reason
    resolved = readonly.resolve(cwd=root)
    target = outside.as_posix()
    write_target = target
    if capability.backend in {"docker", "podman"}:
        target = next(_container_readable_target(path, index, resolved, root)[0]
                      for index, path in enumerate(resolved.readable_roots) if path == outside)
        write_resolved = write.resolve(cwd=root)
        write_target = next(_container_readable_target(path, index, write_resolved, root)[0]
                            for index, path in enumerate(write_resolved.readable_roots) if path == outside)
    script = (
        "#!/bin/sh\necho MINICODE_FSMONITOR_ATTEMPTED >&2\n"
        f"if [ -d /workspace/.git ]; then if [ -d '{target}' ]; then target='{target}'; else target='{write_target}'; fi; else target='{outside.as_posix()}'; fi\n"
        "printf escaped > \"$target/marker\"\nprintf 'token\\0'\n"
    )
    monitor = root / "fsmonitor.sh"
    monitor.write_text(script, encoding="utf-8", newline="\n")
    monitor.chmod(0o755)
    host("config", "core.fsmonitor", "./fsmonitor.sh")
    baseline = host("status", "--short")
    assert b"MINICODE_FSMONITOR_ATTEMPTED" in baseline.stderr
    assert (outside / "marker").read_text() == "escaped", "trusted temp host baseline must prove the executable really ran"
    (outside / "marker").unlink()
    captured = []
    async def capture(argv, **kwargs):
        result = await _run_git(argv, **kwargs)
        captured.append(result)
        return result
    monkeypatch.setattr(git_tools, "_run_git", capture)
    context = ToolExecutionContext(PermissionContext(mode="plan", allow_unsandboxed_commands=False),
                                   workspace_root=root, sandbox_policy=readonly)
    result = await git_tools.GitStatusTool(root).execute({}, context)
    assert not result.is_error, result.content
    assert b"MINICODE_FSMONITOR_ATTEMPTED" in captured[-1].stderr
    assert not (outside / "marker").exists()
    assert not (root / ".git" / "index.lock").exists()
    for tool, args in [(git_tools.GitDiffTool(root), {}), (git_tools.GitLogTool(root), {})]:
        result = await tool.execute(args, context)
        assert not result.is_error, result.content
    hook = root / ".git" / "hooks" / "pre-commit"
    hook.write_text(
        "#!/bin/sh\necho MINICODE_COMMIT_HOOK_ATTEMPTED >&2\n"
        f"if [ -d /workspace/.git ]; then if [ -d '{target}' ]; then target='{target}'; else target='{write_target}'; fi; else target='{outside.as_posix()}'; fi\n"
        "printf escaped > \"$target/marker\"\n"
        "printf poisoned > .minicode/hook-marker\nexit 0\n", encoding="utf-8", newline="\n")
    hook.chmod(0o755)
    (root / "tracked.py").write_text("final\n", encoding="utf-8")
    approved = ToolExecutionContext(PermissionContext(), workspace_root=root, sandbox_policy=write)
    message = "literal ' quote $variable ; \" text"
    result = await git_tools.GitCommitTool(root).execute({"message": message, "add_all": True}, approved)
    assert not result.is_error, result.content
    assert b"MINICODE_COMMIT_HOOK_ATTEMPTED" in captured[-1].stderr
    assert not (outside / "marker").exists()
    assert not (root / ".minicode" / "hook-marker").exists()
    assert (root / ".minicode" / "rules.json").read_text() == "unchanged"
    actual = host("log", "-1", "--format=%s").stdout.decode().strip()
    assert actual == message
    print("GIT_REAL_ORACLE=" + json.dumps({"backend": capability.backend, "host_baseline_outside_write": True,
          "readonly_fsmonitor_ran": True, "readonly_outside_write": False, "commit_hook_ran": True,
          "commit_outside_write": False, "protected_rules_write": False, "literal_commit_message": actual}))


@_git_launch_review_sync
async def test_git_launch_review_real_cancel_stops_hook_before_late_mutation(tmp_path):
    import asyncio
    import json
    import subprocess
    import pytest
    from backend.runtime_env import sanitized_git_env
    from backend.permissions.context import PermissionContext, ToolExecutionContext
    from backend.sandbox import SandboxPolicy, SandboxRunner
    from backend.tools.git_support import _run_git

    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init"], cwd=root, env=sanitized_git_env(),
                   capture_output=True, timeout=10, check=True)
    (root / "tracked").write_text("fixture", encoding="utf-8")
    monitor = root / "slow-monitor.sh"
    monitor.write_text("#!/bin/sh\nprintf started > hook-started\nsleep 60\nprintf late > hook-late\nprintf 'token\\0'\n",
                       encoding="utf-8", newline="\n")
    monitor.chmod(0o755)
    policy = SandboxPolicy.workspace_default(root, timeout=10)
    capability = SandboxRunner(policy).capability(cwd=root)
    assert capability.available and capability.filesystem_isolated, capability.reason
    event = asyncio.Event()
    context = ToolExecutionContext(PermissionContext(), workspace_root=root, sandbox_policy=policy, cancel_event=event)
    task = asyncio.create_task(_run_git(["git", "-c", "core.fsmonitor=./slow-monitor.sh", "status", "--short"],
                                       root=root, context=context))
    try:
        async def wait_for_started():
            while not (root / "hook-started").exists():
                if task.done():
                    result = task.result()
                    pytest.fail(f"real hook never started: {result.stderr!r}")
                await asyncio.sleep(0.02)
        await asyncio.wait_for(wait_for_started(), 10)
    finally:
        event.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 10)
    assert not (root / "hook-late").exists()
    print("GIT_CANCEL_ORACLE=" + json.dumps({"backend": capability.backend, "actual_hook_started": True,
          "late_mutation": False, "actual_cleanup_settled_before_task_done": True}))



def test_git_linked_review_metadata_admission_before_host_read_and_native_unchanged(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from pathlib import Path
    import pytest
    from backend.tools import git_support
    from backend.sandbox import SandboxPolicy

    root = tmp_path / "linked"
    root.mkdir()
    common = tmp_path / "main" / ".git"
    git_dir = common / "worktrees" / "linked"
    git_dir.mkdir(parents=True)
    marker = root / ".git"
    marker.write_text("gitdir: " + str(git_dir) + "\n", encoding="utf-8")
    common_file = git_dir / "commondir"
    common_file.write_text("../..\n", encoding="utf-8")
    paths = {root: "/workspace", git_dir: "/declared/main-git/worktrees/linked", common: "/declared/main-git"}
    class Mapper:
        def __init__(self, policy):
            pass
        def capability(self, *, cwd=None):
            return SimpleNamespace(backend="docker")
        def map_path_to_sandbox(self, path):
            return paths[path]
    monkeypatch.setattr(git_support, "SandboxRunner", Mapper)
    policy = SandboxPolicy(workspace_root=root, readable_roots=(common,))
    original_env = dict(policy.env_overrides)
    argv = ["git", "status", "--short"]
    dispatched, common_path = git_support._portable_git_dispatch(policy, root, root, argv)
    assert dispatched == ["git", "--git-dir=/declared/main-git/worktrees/linked", "--work-tree=/workspace", "status", "--short"]
    assert common_path == "/declared/main-git"
    assert policy.env_overrides == original_env

    reads = []
    read_text = Path.read_text
    def observe_read(path, *args, **kwargs):
        reads.append(path)
        return read_text(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", observe_read)
    denied_marker = SandboxPolicy(workspace_root=root, readable_roots=(common,), denied_roots=(marker,))
    with pytest.raises(PermissionError, match="metadata read denied"):
        git_support._portable_git_dispatch(denied_marker, root, root, argv)
    assert not reads
    denied_common = SandboxPolicy(workspace_root=root, readable_roots=(common,), denied_roots=(common_file,))
    with pytest.raises(PermissionError, match="metadata read denied"):
        git_support._portable_git_dispatch(denied_common, root, root, argv)
    assert reads == [marker]
    reads.clear()
    denied_target = SandboxPolicy(workspace_root=root, denied_roots=(common,))
    with pytest.raises(PermissionError, match="metadata read denied"):
        git_support._portable_git_dispatch(denied_target, root, root, argv)
    assert reads == [marker]

    class Native(Mapper):
        def capability(self, *, cwd=None):
            return SimpleNamespace(backend="windows-elevated-wfp")
    monkeypatch.setattr(git_support, "SandboxRunner", Native)
    reads.clear()
    returned, common_path = git_support._portable_git_dispatch(policy, root, root, argv)
    assert returned is argv and common_path is None
    assert not reads


@_git_launch_review_sync
async def test_git_linked_review_common_env_is_owned_child_only(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from backend.tools import git_support
    from backend.sandbox import SandboxPolicy

    seen = []
    snapshot = SandboxPolicy(workspace_root=tmp_path, env_overrides={"VISIBLE": "captured"})
    common = "/declared/a'quote$literal;path"
    def dispatch(policy, root, cwd, argv):
        assert policy is snapshot
        return [argv[0], "--git-dir=/declared/worktree", "--work-tree=/workspace", *argv[1:]], common
    class Runner:
        def __init__(self, policy):
            assert policy is snapshot
        async def spawn_shell_interactive(self, command, **kwargs):
            seen.append(command)
            return SimpleNamespace(returncode=0)
        async def cleanup(self):
            return True
    async def communicate(process, **kwargs):
        return b"", b""
    monkeypatch.setattr(git_support, "_portable_git_dispatch", dispatch)
    monkeypatch.setattr(git_support, "SandboxRunner", Runner)
    monkeypatch.setattr(git_support, "communicate_bounded", communicate)
    await git_support._run_git(["git", "status"], root=tmp_path, sandbox_policy=snapshot)
    assert "GIT_COMMON_DIR" in seen[0]
    assert "--git-dir=/declared/worktree" in seen[0]
    assert "GIT_COMMON_DIR" not in snapshot.env_overrides
    if git_support.os.name == "nt":
        assert seen[0].startswith("$env:GIT_COMMON_DIR='/declared/a''quote$literal;path'; & ")
    else:
        import shlex
        assert shlex.split(seen[0])[0] == "GIT_COMMON_DIR=" + common


@_git_launch_review_sync
async def test_git_linked_review_real_status_diff_prompt_commit_and_cancel(tmp_path):
    import asyncio
    import hashlib
    import json
    import subprocess
    import pytest
    from backend.runtime_env import sanitized_git_env
    from backend.agent import prompting
    from backend.permissions.context import PermissionContext, ToolExecutionContext
    from backend.sandbox import SandboxPolicy, SandboxRunner
    from backend.tools.git_tools import GitStatusTool, GitDiffTool, GitLogTool, GitCommitTool
    from backend.tools.git_support import _run_git

    primary = tmp_path / "primary"
    primary.mkdir()
    linked = tmp_path / "linked"
    outside = tmp_path / "outside"
    outside.mkdir()
    def host(cwd, *args):
        return subprocess.run(["git", *args], cwd=cwd, env=sanitized_git_env(),
                              capture_output=True, timeout=10, check=True)
    host(primary, "init")
    host(primary, "config", "user.name", "Linked Boundary Review")
    host(primary, "config", "user.email", "linked-boundary@example.invalid")
    (primary / "tracked.py").write_text("initial\n", encoding="utf-8")
    host(primary, "add", "-A")
    host(primary, "commit", "-m", "fixture")
    original_primary_head = host(primary, "rev-parse", "HEAD").stdout
    host(primary, "worktree", "add", "-b", "linked-review", str(linked))
    marker_bytes = (linked / ".git").read_bytes()
    git_dir = primary / ".git" / "worktrees" / "linked"
    common = primary / ".git"
    common_bytes = (git_dir / "commondir").read_bytes()
    readonly = SandboxPolicy(workspace_root=linked, readable_roots=(common, outside))
    capability = SandboxRunner(readonly).capability(cwd=linked)
    assert capability.available and capability.filesystem_isolated, capability.reason
    mapper = SandboxRunner(readonly)
    assert mapper.map_path_from_sandbox(mapper.map_path_to_sandbox(git_dir)) == str(git_dir)
    assert mapper.map_path_from_sandbox(mapper.map_path_to_sandbox(common)) == str(common)
    mapped_outside = mapper.map_path_to_sandbox(outside)
    monitor = linked / "fsmonitor.sh"
    monitor.write_text(
        "#!/bin/sh\necho LINKED_FSMONITOR_ATTEMPTED >&2\n"
        f"if [ -d /workspace ]; then target='{mapped_outside}'; else target='{outside.as_posix()}'; fi\n"
        "printf escaped > \"$target/marker\"\nprintf 'token\\0/\\0'\n",
        encoding="utf-8", newline="\n")
    monitor.chmod(0o755)
    host(primary, "config", "core.fsmonitor", "./fsmonitor.sh")
    baseline = host(linked, "status", "--short")
    assert b"LINKED_FSMONITOR_ATTEMPTED" in baseline.stderr
    assert (outside / "marker").read_text() == "escaped"
    (outside / "marker").unlink()
    (linked / "tracked.py").write_text("linked final\n", encoding="utf-8")
    context = ToolExecutionContext(PermissionContext(mode="plan", allow_unsandboxed_commands=False),
                                   workspace_root=linked, sandbox_policy=readonly)
    for tool, args in [(GitStatusTool(linked), {}), (GitDiffTool(linked), {"file_path": "tracked.py"}),
                       (GitLogTool(linked), {})]:
        result = await tool.execute(args, context)
        assert not result.is_error, result.content
        if tool.name == "git_status":
            assert "M tracked.py" in result.content
        if tool.name == "git_diff":
            assert "+linked final" in result.content
    raw = await _run_git(["git", "status", "--short"], root=linked, context=context)
    assert b"LINKED_FSMONITOR_ATTEMPTED" in raw.stderr
    prompt = await prompting.build_git_status_context_async(linked, context=context)
    assert "Current branch: linked-review" in prompt and "M tracked.py" in prompt
    assert not (outside / "marker").exists()

    no_external_write = SandboxPolicy(workspace_root=linked, writable_roots=(linked,), readable_roots=(common, outside))
    refused_context = ToolExecutionContext(PermissionContext(), workspace_root=linked, sandbox_policy=no_external_write)
    before_index = hashlib.sha256((git_dir / "index").read_bytes()).hexdigest()
    refused = await GitCommitTool(linked).execute({"message": "must not grant external", "add_all": True}, refused_context)
    assert refused.is_error and "explicit write authority" in refused.content
    assert hashlib.sha256((git_dir / "index").read_bytes()).hexdigest() == before_index

    authorized = SandboxPolicy(workspace_root=linked,
                               writable_roots=(linked, common, git_dir), readable_roots=(outside,))
    approved = ToolExecutionContext(PermissionContext(), workspace_root=linked, sandbox_policy=authorized)
    committed = await GitCommitTool(linked).execute({"message": "linked approved", "add_all": True}, approved)
    assert not committed.is_error, committed.content
    assert host(linked, "log", "-1", "--format=%s").stdout.strip() == b"linked approved"
    assert host(primary, "rev-parse", "HEAD").stdout == original_primary_head
    assert (linked / ".git").read_bytes() == marker_bytes
    assert (git_dir / "commondir").read_bytes() == common_bytes
    assert not (outside / "marker").exists()

    monitor.write_text("#!/bin/sh\nprintf started > linked-hook-started\nsleep 60\nprintf late > linked-hook-late\nprintf 'token\\0/\\0'\n",
                       encoding="utf-8", newline="\n")
    approved.cancel_event = asyncio.Event()
    task = asyncio.create_task(_run_git(["git", "status", "--short"], root=linked, context=approved))
    try:
        async def started():
            while not (linked / "linked-hook-started").exists():
                if task.done():
                    result = task.result()
                    pytest.fail(f"linked hook did not start: {result.stderr!r}")
                await asyncio.sleep(0.02)
        await asyncio.wait_for(started(), 10)
    finally:
        approved.cancel_event.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 10)
    assert not (linked / "linked-hook-late").exists()
    print("GIT_LINKED_REAL_ORACLE=" + json.dumps({"backend": capability.backend,
          "host_positive_outside_write": True, "status_diff_log_prompt": True,
          "outside_write": False, "read_only_external_metadata_commit_refused": True,
          "already_authorized_external_write_commit": True, "primary_head_unchanged": True,
          "gitfile_commondir_bytes_unchanged": True, "actual_cancel_hook_started": True,
          "late_mutation": False, "actual_cleanup_settled": True}))
