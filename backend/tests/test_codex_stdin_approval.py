from __future__ import annotations

import asyncio
import json
import os
import shlex
import sys
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.agent.context import ContextBuilder
from backend.agent.code_execution import CodeExecutionRuntime
from backend.agent.code_execution_store import CodeExecutionStore
from backend.agent.run_context import RunContext
from backend.agent.state import AgentState
from backend.agent.tool_batch_runner import ToolBatchRunner
from backend.agent.tool_execution import store_result
from backend.artifact.store import ArtifactStore
from backend.config import PermissionSettings, permission_settings_from_config
from backend.hooks.manager import HookResult
from backend.llm.base import ToolCallEvent
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.permissions.grants import PermissionGrantResponse
from backend.sandbox import SandboxPolicy, SandboxRunner
from backend.sandbox.policy import AdditionalPermissionProfile, FileSystemAccessMode, FileSystemPath, FileSystemPermissions, FileSystemSandboxEntry, NetworkPermissions, NetworkSandboxPolicy
from backend.sandbox.result import SandboxResult
from backend.terminal.manager import BackgroundCommand, BackgroundCommandManager
from backend.terminal.stdin_authority import StdinAuthorityError, TerminalInputApproval, TerminalLaunchAuthority
from backend.tools.command_support import stdin_approval_enabled, stdin_wait_ms
from backend.tools.command_tool import RunCommandTool
from backend.tools.monitor_tool import MonitorTool
from backend.tools.registry import ToolRegistry
from backend.tools.toolsets import ACTIVE_TOOLSET_POLICY_METADATA_KEY, ToolsetPolicy


class InputWriter:
    def __init__(self):
        self.writes = []
        self.closed = False
        self.on_drain = None

    def write(self, chars):
        self.writes.append(chars)

    async def drain(self):
        if self.on_drain is not None:
            self.on_drain()

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass


def retained_terminal(tmp_path, *, intent="require_escalated", tty=False, policy=None):
    policy = policy or SandboxPolicy.workspace_default(tmp_path)
    manager = BackgroundCommandManager(session_id="stdin-session")
    process = SimpleNamespace(pid=12345, returncode=None, stdin=InputWriter())
    authority = TerminalLaunchAuthority.capture(baseline_policy=policy, effective_policy=policy,
        launch_permissions=intent, environment_id="local")
    command = BackgroundCommand(command_id="terminal", command="original command", cwd=str(tmp_path),
        conversation_id="owner", tty=tty, terminal_authority=authority, original_tool_call_id="original-exec",
        pid=process.pid, runner=SimpleNamespace(_kill_tree=AsyncMock(return_value=True)))
    manager._commands[command.command_id] = command
    manager._processes[command.command_id] = process
    context = ToolExecutionContext(PermissionContext(mode="auto"), session_id="stdin-session", conversation_id="owner",
        workspace_root=tmp_path, sandbox_policy=policy, background_manager=manager, tool_call_id="input-call",
        authorization_digest="frozen-input", authorization_policy_identity="frozen-policy", turn_id="turn")
    return manager, command, process, context


def input_action(**changes):
    action = TerminalInputApproval("original-exec", "input-call", "local", "terminal", 12345,
        "hello\n", "C:/repo", True, "require_escalated", None, "retained authority")
    return replace(action, **changes)


@pytest.mark.parametrize("kind,expected", [
    ("baseline", "use_default"), ("full_access_baseline", "use_default"),
    ("escalated", "require_escalated"), ("filesystem_escalated", "require_escalated"),
    ("agent_grant", "with_additional_permissions"), ("internal_grant", "use_default"),
    ("empty_grant", "use_default"),
])
def test_retained_launch_authority_matches_source(tmp_path, kind, expected):
    policy = SandboxPolicy.bypass() if kind == "full_access_baseline" else SandboxPolicy.workspace_default(tmp_path)
    extra = AdditionalPermissionProfile(network=NetworkPermissions(enabled=True))
    additional = extra if kind == "agent_grant" else AdditionalPermissionProfile() if kind == "empty_grant" else None
    internal = extra if kind == "internal_grant" else None
    effective = policy.with_additional_permissions(extra) if kind in {"agent_grant", "internal_grant"} else policy
    authority = TerminalLaunchAuthority.capture(baseline_policy=policy, effective_policy=effective,
        launch_permissions="require_escalated" if kind == "escalated" else "use_default",
        filesystem_escalated=kind == "filesystem_escalated", additional_permissions=additional,
        internal_permissions=internal)
    assert authority.review_requirement(policy, environment_id="local") == expected
    if kind == "empty_grant":
        assert authority.additional_permissions is None


def test_denied_reads_reject_old_terminal_but_network_drift_only_requests_review(tmp_path):
    policy = SandboxPolicy(workspace_root=tmp_path, writable_roots=(tmp_path,), denied_roots=(tmp_path / "secret",))
    authority = TerminalLaunchAuthority.capture(baseline_policy=policy, effective_policy=policy)
    network_drift = replace(policy, permission_profile=replace(policy.permission_profile, network=NetworkSandboxPolicy.ENABLED))
    assert authority.review_requirement(network_drift, environment_id="local") == "require_escalated"
    filesystem_drift = replace(policy, policy_cwd=tmp_path / "other")
    with pytest.raises(StdinAuthorityError, match="start a new terminal"):
        authority.review_requirement(filesystem_drift, environment_id="local")
    bypass = replace(authority, launch_permissions="require_escalated")
    with pytest.raises(StdinAuthorityError, match="denied-read"):
        bypass.review_requirement(policy, environment_id="local")
    with pytest.raises(StdinAuthorityError, match="original environment"):
        authority.review_requirement(policy, environment_id="other")


def test_internal_grants_and_proxy_settings_are_never_disclosed(tmp_path, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://private-token@internal.invalid")
    policy = SandboxPolicy.workspace_default(tmp_path)
    private = AdditionalPermissionProfile(file_system=FileSystemPermissions((FileSystemSandboxEntry(
        FileSystemPath.path(tmp_path / "private-internal-root"), FileSystemAccessMode.WRITE),)))
    authority = TerminalLaunchAuthority.capture(baseline_policy=policy, effective_policy=policy.with_additional_permissions(private), internal_permissions=private)
    monkeypatch.setenv("HTTP_PROXY", "http://different-token@internal.invalid")
    assert authority.review_requirement(policy, environment_id="local") == "require_escalated"
    reason = authority.approval_reason("require_escalated")
    payload = input_action(reason=reason).to_payload()
    rendered = json.dumps(payload)
    assert "internal filesystem grant" in reason
    assert "private-internal-root" not in rendered and "private-token" not in rendered and "different-token" not in rendered


def test_review_limit_measures_full_guardian_json_and_reason_at_8000_bytes():
    action = input_action(input="")
    base = len(json.dumps(action.guardian_review_payload(), ensure_ascii=False, sort_keys=True, indent=2).encode()) + len(action.reason.encode())
    exact = replace(action, input="x" * (8000 - base))
    exact.verify_reviewable()
    with pytest.raises(StdinAuthorityError, match="too large"):
        replace(exact, input=exact.input + "x").verify_reviewable()
    with pytest.raises(StdinAuthorityError, match="too large"):
        replace(action, input="\x1b" * 1500).verify_reviewable()
    with pytest.raises(StdinAuthorityError, match="too large"):
        replace(action, input="中" * 3000).verify_reviewable()
    with pytest.raises(StdinAuthorityError, match="NUL"):
        replace(action, input="first\0unreviewed tail").verify_reviewable()
    assert action.guardian_review_payload() == {"tool": "write_stdin", "session_id": 12345, "chars": "",
        "cwd": "C:/repo", "tty": True, "sandbox_permissions": "require_escalated", "environment_id": "local"}
    assert action.permission_hook_input() == {"session_id": 12345, "chars": "", "parent_call_id": "original-exec",
        "approval_id": "input-call", "environment_id": "local", "cwd": "C:/repo", "tty": True,
        "sandbox_permissions": "require_escalated", "additional_permissions": None}


@pytest.mark.parametrize("response,approved", [
    ({"action": "approve"}, True), ({"action": "reject"}, False),
    ({"action": "approve", "remember": True}, False), ({"action": "approve", "scope": "session"}, False),
    ({"action": "approve", "updated_input": {"chars": "different"}}, False),
    ({"action": "approve", "request_digest": "other-request"}, False),
])
@pytest.mark.asyncio
async def test_manual_review_binds_exact_input_and_never_remembers(tmp_path, response, approved):
    manager, command, process, context = retained_terminal(tmp_path)
    context.emit_event = AsyncMock()
    context.approval_handler = AsyncMock(return_value=response)
    result = await MonitorTool().execute({"action": "write_stdin", "command_id": command.command_id, "chars": "hello\n"}, context)
    assert result.is_error is not approved
    assert process.stdin.writes == ([b"hello\n"] if approved else [])
    payload = context.emit_event.await_args.args[1]
    assert payload["subtype"] == "write_stdin" and payload["request_digest"] == "frozen-input"
    assert payload["policy_identity"] == "frozen-policy"
    assert payload["stdin_action"]["id"] == "original-exec" and payload["stdin_action"]["approval_id"] == "input-call"
    assert shlex.split(payload["stdin_action"]["display_command"]) == ["write_stdin", "--session-id", "12345", "hello\n"]
    assert result.runtime_metadata["terminal_hook"]["ready"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["never", "no_channel", "cancelled"])
async def test_no_write_when_approval_is_unavailable_or_cancelled(tmp_path, kind):
    manager, command, process, context = retained_terminal(tmp_path)
    if kind == "never":
        context.permission = replace(context.permission, approval_policy="never")
    elif kind == "cancelled":
        context.cancel_event = asyncio.Event(); context.cancel_event.set()
    if kind == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            await manager.write_stdin(command.command_id, "input", conversation_id="owner", context=context)
    else:
        result = await MonitorTool().execute({"action": "write_stdin", "command_id": command.command_id, "chars": "input"}, context)
        assert result.is_error and ("never" if kind == "never" else "no approval channel") in result.content
    assert process.stdin.writes == []


@pytest.mark.asyncio
@pytest.mark.parametrize("hook_result,approved", [
    (HookResult(permission_decision="allow"), True), (HookResult(permission_decision="deny", message="hook denies"), False),
    (HookResult(updated_input={"chars": "changed"}), False),
])
async def test_source_permission_hook_receives_transport_input_before_manual_review(tmp_path, hook_result, approved):
    manager, command, process, context = retained_terminal(tmp_path)
    hook = SimpleNamespace(run_permission_request=AsyncMock(return_value=hook_result))
    context.run_context = RunContext(hook_manager=hook)
    context.emit_event = AsyncMock(); context.approval_handler = AsyncMock()
    result = await MonitorTool().execute({"action": "write_stdin", "command_id": command.command_id, "chars": "hello\n"}, context)
    assert result.is_error is not approved
    assert process.stdin.writes == ([b"hello\n"] if approved else [])
    assert hook.run_permission_request.await_args.args[:2] == ("write_stdin", input_action(cwd=str(tmp_path), tty=False).permission_hook_input())
    context.approval_handler.assert_not_awaited(); context.emit_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_guardian_routes_full_input_and_reason_without_second_manual_approval(tmp_path):
    manager, command, process, context = retained_terminal(tmp_path)
    service = SimpleNamespace(decide=AsyncMock(return_value=SimpleNamespace(require_user=False, status="approved", rationale="authorized")))
    context.run_context = RunContext(guardian_review_service=service)
    context.emit_event = AsyncMock(); context.approval_handler = AsyncMock()
    result = await MonitorTool().execute({"action": "write_stdin", "command_id": command.command_id, "chars": "hello\n"}, context)
    assert not result.is_error and process.stdin.writes == [b"hello\n"]
    guardian_action = service.decide.await_args.args[0]
    assert json.loads(guardian_action.json_payload) == input_action(cwd=str(tmp_path), tty=False).guardian_review_payload()
    assert guardian_action.call_id == "original-exec" and guardian_action.approval_id == "input-call"
    assert "outside the sandbox" in service.decide.await_args.kwargs["approval_reason"]
    context.approval_handler.assert_not_awaited(); context.emit_event.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", ["command", "process"])
async def test_approval_cannot_write_to_replaced_instance_even_if_pid_is_reused(tmp_path, replacement):
    manager, command, process, context = retained_terminal(tmp_path)
    async def approve(_action):
        if replacement == "command":
            manager._commands[command.command_id] = replace(command)
        else:
            manager._processes[command.command_id] = SimpleNamespace(pid=process.pid, returncode=None, stdin=InputWriter())
    with pytest.raises(StdinAuthorityError, match="instance changed"):
        await manager.write_stdin(command.command_id, "input", conversation_id="owner", context=context, authorize_input=approve)
    assert process.stdin.writes == []


@pytest.mark.asyncio
async def test_queued_write_rechecks_current_policy_and_strict_review_under_same_lock(tmp_path):
    manager, command, process, context = retained_terminal(tmp_path, intent="use_default")
    lock = asyncio.Lock(); await lock.acquire(); manager._stdin_locks[command.command_id] = lock
    reviews = []
    async def approve(action):
        assert lock.locked()
        reviews.append(action)
    queued = asyncio.create_task(manager.write_stdin(command.command_id, "input", conversation_id="owner", context=context, authorize_input=approve))
    await asyncio.sleep(0)
    context.permission_grants.record(PermissionGrantResponse(AdditionalPermissionProfile(network=NetworkPermissions(enabled=True)), strict_auto_review=True), "local", "turn")
    lock.release()
    assert await queued == 5
    assert len(reviews) == 1 and reviews[0].sandbox_permissions == "use_default"
    context.sandbox_policy = replace(context.sandbox_policy, policy_cwd=tmp_path / "other")
    await manager.write_stdin(command.command_id, "again", conversation_id="owner", context=context, authorize_input=approve)
    assert reviews[-1].sandbox_permissions == "require_escalated"


@pytest.mark.asyncio
async def test_poll_wait_cannot_overlap_pending_stdin_approval(tmp_path):
    manager, command, process, context = retained_terminal(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    async def approve(action):
        entered.set(); await release.wait()
    write = asyncio.create_task(manager.write_stdin(command.command_id, "input", conversation_id="owner", context=context, authorize_input=approve))
    await entered.wait()
    poll = asyncio.create_task(manager.wait(command.command_id, conversation_id="owner", wait_ms=0))
    await asyncio.sleep(0)
    assert not poll.done()
    release.set(); await write; assert await poll is command


@pytest.mark.asyncio
async def test_empty_poll_and_pipe_interrupt_skip_review_and_never_write_etx_to_pipe(tmp_path, monkeypatch):
    manager, command, process, context = retained_terminal(tmp_path)
    approval = AsyncMock()
    await manager.write_stdin(command.command_id, "", conversation_id="owner", context=context, authorize_input=approval)
    if os.name != "nt":
        signals = []; monkeypatch.setattr("backend.terminal.manager.os.killpg", lambda pid, sig: signals.append((pid, sig)))
    await manager.write_stdin(command.command_id, "\x03", conversation_id="owner", context=context, authorize_input=approval)
    approval.assert_not_awaited(); assert process.stdin.writes == []
    if os.name == "nt":
        command.runner._kill_tree.assert_awaited_once_with(process)
    else:
        assert signals[0][0] == process.pid
    command.status = "completed"; command.command_completed = True; command.exit_code = 0
    empty = await MonitorTool().execute({"action": "write_stdin", "command_id": command.command_id, "chars": ""}, context)
    assert not empty.is_error and empty.runtime_metadata["terminal_hook"]["ready"]
    assert MonitorTool().validate_input({"action": "write_stdin", "command_id": command.command_id, "chars": ""}) == ""


@pytest.mark.asyncio
async def test_disabled_feature_preserves_existing_pipe_stdin_and_close_contract(tmp_path, monkeypatch):
    manager, command, process, context = retained_terminal(tmp_path)
    monkeypatch.setattr("backend.tools.command_support.stdin_approval_enabled", lambda _context: False)
    approval = AsyncMock()
    assert await manager.write_stdin(command.command_id, "你好\n", conversation_id="owner", close_stdin=True, context=context, authorize_input=approval) == 7
    assert process.stdin.writes == ["你好\n".encode()] and process.stdin.closed
    approval.assert_not_awaited()
    with pytest.raises(KeyError):
        await manager.write_stdin(command.command_id, "foreign", conversation_id="other", context=context)


@pytest.mark.parametrize("chars,value,expected", [("input", None, 250), ("input", 0, 250), ("input", 60000, 30000), ("", 0, 5000), ("", 60000, 60000), ("", 2**64 - 1, 300000)])
def test_source_stdin_yield_defaults_and_clamps(tmp_path, chars, value, expected):
    context = ToolExecutionContext(PermissionContext(), workspace_root=tmp_path)
    assert stdin_wait_ms(value, chars=chars, context=context) == expected
    assert stdin_approval_enabled(context)


@pytest.mark.parametrize("value", [True, -1, 0.5, "1000", 2**64])
def test_stdin_yield_requires_source_unsigned_integer(tmp_path, value):
    context = ToolExecutionContext(PermissionContext(), workspace_root=tmp_path)
    with pytest.raises(ValueError, match="non-negative integer"):
        stdin_wait_ms(value, chars="input", context=context)


@pytest.mark.asyncio
async def test_write_returns_real_output_and_process_completion_claims_bash_once(tmp_path):
    manager, command, process, context = retained_terminal(tmp_path, intent="use_default")
    def finished():
        command.append_live_output("actual stderr and stdout\n")
        command.status = "failed"; command.command_completed = True; command.exit_code = 7; process.returncode = 7
    process.stdin.on_drain = finished
    result = await MonitorTool().execute({"action": "write_stdin", "command_id": command.command_id, "chars": "finish\n"}, context)
    assert not result.is_error and result.status == "success"
    assert "actual stderr and stdout" in result.content and "Wrote" not in result.content
    assert result.runtime_metadata["process_status"] == "failed" and result.runtime_metadata["exit_code"] == 7
    hook = result.runtime_metadata["terminal_hook"]
    assert hook["tool_use_id"] == "original-exec" and hook["tool_input"] == {"command": "original command"}
    assert hook["output"] == "actual stderr and stdout\n"
    replay = await MonitorTool().execute({"command_id": command.command_id}, context)
    assert replay.runtime_metadata["terminal_hook"]["ready"] is False
    builder, state = ContextBuilder(), AgentState(user_message="fixture")
    event = store_result(ToolCallEvent(id="input-call", name="monitor", arguments={"action": "write_stdin"}), result, builder, state, tool_ctx=context)
    assert event.data["status"] == "success" and not event.data["is_error"]
    assert not event.data.get("error_kind") and not event.data.get("model_observation")
    assert not state.tool_calls[-1].error_kind


@pytest.mark.asyncio
async def test_runtime_failure_retains_error_and_never_claims_completed_bash(tmp_path, monkeypatch):
    runner = SandboxRunner(SandboxPolicy.bypass())
    failed = await runner.run("missing executable", argv=[str(tmp_path / "missing-executable")], cwd=str(tmp_path))
    assert failed.execution_error and failed.exit_code == 127 and not failed.success
    store = ArtifactStore(storage_dir=tmp_path / "artifacts")
    monkeypatch.setattr(SandboxRunner, "run", AsyncMock(return_value=failed))
    context = ToolExecutionContext(PermissionContext(mode="bypass"), workspace_root=tmp_path)
    try:
        result = await RunCommandTool(store)._execute_foreground("missing executable", str(tmp_path), 10, context)
        assert result.is_error and result.error_kind == "execution_error"
        assert result.runtime_metadata["command_completed"] is False
        manager, command, process, monitor_context = retained_terminal(tmp_path)
        command.status = "failed"; command.exit_code = failed.exit_code
        command.append_live_output(failed.stderr)
        snapshot = await MonitorTool().execute({"command_id": command.command_id}, monitor_context)
        assert snapshot.is_error and not snapshot.runtime_metadata["terminal_hook"]["ready"]
    finally:
        store.shutdown()


@pytest.mark.asyncio
async def test_real_code_vm_nested_stdin_keeps_original_command_and_distinct_approval_id(tmp_path):
    manager, command, process, context = retained_terminal(tmp_path)
    def finished():
        command.append_live_output("nested real tool output\n")
        command.status = "completed"; command.command_completed = True; command.exit_code = 0; process.returncode = 0
    process.stdin.on_drain = finished
    published = []
    async def publish(event):
        published.append(event)
    context.run_context = RunContext(code_store=CodeExecutionStore(), publish_nested_event=publish)
    context.tool_call_id = "code-parent"
    context.approval_handler = AsyncMock(return_value={"action": "approve"})
    registry = ToolRegistry(); registry.register(MonitorTool()); context.tool_registry = registry
    context.metadata[ACTIVE_TOOLSET_POLICY_METADATA_KEY] = ToolsetPolicy()
    builder, state = ContextBuilder(), AgentState(user_message="fixture")
    runtime = CodeExecutionRuntime(context=builder, state=state, tool_context=context,
        permission_checker=PermissionChecker(PermissionSettings(), tmp_path), skill_manager=None,
        turn_kernel=SimpleNamespace(refresh_live_permission_context=lambda: None))
    try:
        result = await runtime.execute("const r = await tools.monitor({action:'write_stdin',command_id:'terminal',chars:'hello\\n'}); text(r.content);", context, yield_time_ms=10000)
        cell = next(iter(runtime.cells.values()))
        await cell.task
        assert not result.is_error and cell.status == "completed" and process.stdin.writes == [b"hello\n"]
        approval = next(event for event in published if event.type == "approval_request")
        action = approval.data["stdin_action"]
        assert action["id"] == "original-exec" and action["approval_id"].startswith("code_")
        assert action["approval_id"] != "code-parent" and action["input"] == "hello\n"
        assert approval.data["call_source"]["kind"] == "code_mode"
        assert approval.data["request_digest"] and approval.data["policy_identity"]
        assert state.tool_calls[-1].call_source["kind"] == "code_mode" and not state.tool_calls[-1].error_kind
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_real_batch_pairs_one_bash_hook_for_original_command_after_stdin_and_exit_7(tmp_path):
    code = "import sys; print('READY',flush=True); value=sys.stdin.readline(); print('DONE:'+value.strip(),flush=True); sys.exit(7)"
    command_text = ("& '" + sys.executable.replace("'", "''") + "' -u -c '" + code.replace("'", "''") + "'"
                    if sys.platform == "win32" else shlex.quote(sys.executable) + " -u -c " + shlex.quote(code))
    manager = BackgroundCommandManager(session_id="batch-stdin")
    store = ArtifactStore(storage_dir=tmp_path / "artifacts")
    hook = SimpleNamespace(run_pre_tool=AsyncMock(return_value=HookResult()), run_post_tool=AsyncMock(return_value=HookResult()), run_post_tool_failure=AsyncMock(return_value=HookResult()))
    context = ToolExecutionContext(PermissionContext(mode="bypass"), workspace_root=tmp_path, conversation_id="owner",
        session_id="batch-stdin", background_manager=manager, run_context=RunContext(hook_manager=hook))
    registry = ToolRegistry(); registry.register(RunCommandTool(store, manager)); registry.register(MonitorTool())
    builder, state = ContextBuilder(), AgentState(user_message="fixture")
    batch = ToolBatchRunner(ctx=builder, state=state, tool_registry=registry, permission_checker=PermissionChecker(permission_settings_from_config({}), tmp_path), approval_handler=None,
        skill_manager=None, permission_context=context.permission, tool_ctx=context)
    try:
        first = [event async for event in batch.run([ToolCallEvent(id="original-exec", name="run_command", arguments={"command": command_text, "yield_time_ms": 0})])]
        command_id = manager.list_commands(conversation_id="owner")[0]["command_id"]
        assert any(event.type == "tool_result" for event in first)
        await manager.wait(command_id, conversation_id="owner", wait_ms=10000, cursor=0)
        running = [event async for event in batch.run([ToolCallEvent(id="running-poll", name="monitor", arguments={"command_id": command_id})])]
        assert any(event.type == "tool_result" for event in running)
        hook.run_post_tool.assert_not_awaited()
        written = [event async for event in batch.run([ToolCallEvent(id="input-call", name="monitor", arguments={"action": "write_stdin", "command_id": command_id, "chars": "hello\n", "yield_time_ms": 10000})])]
        await manager.wait(command_id, conversation_id="owner", wait_ms=10000)
        final = [event async for event in batch.run([ToolCallEvent(id="final-poll", name="monitor", arguments={"command_id": command_id})])]
        assert hook.run_pre_tool.await_count == 1
        assert hook.run_pre_tool.await_args.args == ("Bash", {"command": command_text})
        assert hook.run_post_tool.await_count == 1
        assert hook.run_post_tool.await_args.args[:2] == ("Bash", {"command": command_text})
        assert "DONE:hello" in hook.run_post_tool.await_args.args[2] and "<untrusted" not in hook.run_post_tool.await_args.args[2]
        assert hook.run_post_tool.await_args.kwargs["tool_call_id"] == "original-exec"
        hook.run_post_tool_failure.assert_not_awaited()
        results = [event for event in written + final if event.type == "tool_result"]
        assert all(event.data["status"] == "success" and not event.data["is_error"] for event in results)
        assert not state.tool_calls[-1].error_kind and manager.get_status(command_id, conversation_id="owner").exit_code == 7
    finally:
        await manager.shutdown(); store.shutdown()
