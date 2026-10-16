"""Real local/Source transports through tool admission and retained commands."""
from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from dataclasses import replace
import os
import sys

import pytest
import pytest_asyncio

from backend.agent.context import ContextBuilder
from backend.agent.environment_runtime import resolve_environment_config, thread_environment_defaults
from backend.agent.environment_selection import EnvironmentConfigState, ThreadEnvironments, TurnEnvironmentRequest, TurnEnvironmentSelection, TurnEnvironmentSnapshot
from backend.agent.final_tool_request import FinalExecutableToolRequest
from backend.agent.state import AgentState
from backend.agent.tool_batch_execution import execute_tool_batch
from backend.environments import EnvironmentManager
from backend.llm.base import ToolCallEvent
from backend.permissions.environment_command import EnvironmentCommandPolicy
from backend.permissions.checker import PermissionChecker
from backend.config import PermissionSettings
from backend.checkpoint.manager import CheckpointManager
from backend.checkpoint.store import CheckpointStore
from backend.permissions.grants import permission_profile_payload
from backend.terminal.manager import BackgroundCommandManager
from backend.terminal.task_persistence import cleanup_orphaned_tasks, load_task, save_task
from backend.tests.test_environments import PEER, configure
from backend.tests.test_native_exec_policy_chain import setup
from backend.tools.edit_file import EditFileTool
from backend.tools.read_file import ReadFileTool
from backend.tools.request_permissions import RequestPermissionsTool
from backend.tools.environment_policy import policy_for_environment_snapshot
from backend.tools.toolsets import ToolsetPolicy
from backend.tools.write_file import WriteFileTool
from backend.workspace.executor_local import LocalExecutionEnvironmentBackend


@pytest_asyncio.fixture(params=("local", "source"))
async def executor_host(request, tmp_path):
    kind = request.param
    if kind == "source" and not os.environ.get("MINICODE_SOURCE_EXECUTOR"):
        pytest.skip("requires an explicitly selected owned Source exec-server peer")
    host = setup(tmp_path, approval_policy="never")
    async with AsyncExitStack() as cleanup:
        cleanup.callback(host.context.artifact_store.shutdown)
        config_dir = tmp_path / "executors"
        config_dir.mkdir()
        if kind == "local":
            configure(config_dir, [], default="local", include_local=True)
            manager = EnvironmentManager(config_dir, local_backend=LocalExecutionEnvironmentBackend())
            environment_id = "local"
        else:
            codex_home = tmp_path / "owned-source-home"
            codex_home.mkdir()
            configure(config_dir, [{"id": "target", "program": os.environ["MINICODE_SOURCE_EXECUTOR"],
                "args": ["exec-server", "--listen", "stdio", "--concurrent-requests", "4"],
                "cwd": str(host.workspace), "env": {"CODEX_HOME": str(codex_home)}}], default="target")
            manager = EnvironmentManager(config_dir)
            environment_id = "target"
        cleanup.push_async_callback(manager.close)
        permission = replace(host.context.permission, mode="bypass", sandbox_mode="danger-full-access")
        owner = ThreadEnvironments(manager, thread_environment_defaults(permission, host.stack), resolve_config=resolve_environment_config)
        cleanup.push_async_callback(owner.aclose)
        cwd = host.workspace.as_uri()
        await owner.update_selections((TurnEnvironmentSelection.from_request(TurnEnvironmentRequest(environment_id, cwd, (cwd,))),))
        snapshot = await owner.snapshot()
        cleanup.push_async_callback(snapshot.aclose)
        assert len(snapshot.turn_environments()) == 1
        host.context.run_context.thread_environments = owner
        host.context.run_context.turn_environments = snapshot
        background = BackgroundCommandManager(session_id="executor-admission-session")
        cleanup.push_async_callback(background.shutdown)
        host.checker = PermissionChecker(PermissionSettings(require_confirm=[], require_diff_review=[]), host.workspace)
        host.context = replace(host.context, permission=permission, sandbox_policy=None,
            background_manager=background, permission_checker=host.checker,
            checkpoint_manager=CheckpointManager(CheckpointStore(tmp_path / "checkpoints")))
        for tool in (ReadFileTool(host.context.artifact_store), WriteFileTool(), EditFileTool(), RequestPermissionsTool()):
            host.registry.register(tool)
        host.kind, host.manager, host.owner, host.environments, host.background = kind, manager, owner, snapshot, background
        host.state = AgentState(user_message="Verify actual executor admission", conversation_id="source-owner", workspace_root=host.workspace)
        host.builder = ContextBuilder(conversation_id="source-owner", workspace_root=host.workspace)
        yield host


async def batch(host, name, arguments, *, call_id=None):
    call = ToolCallEvent(id=call_id or f"executor-{len(host.state.tool_calls)}", name=name, arguments=arguments)
    events = [event async for event in execute_tool_batch([call], ctx=host.builder, state=host.state,
        tool_registry=host.registry, permission_checker=host.checker, approval_handler=host.context.approval_handler,
        skill_manager=None, permission_context=host.context.permission, tool_ctx=host.context)]
    return call, next(event.data for event in reversed(events) if event.type == "tool_result"), events


def print_command(host, text):
    return f"Write-Output '{text}'" if host.environments.primary().peer_info["platformOs"] == "windows" else f"printf '%s\\n' '{text}'"


@pytest.mark.asyncio
async def test_real_tool_batch_freezes_target_argv_and_returns_actual_exit_output(executor_host):
    host = executor_host
    call, result, _ = await batch(host, "run_command", {"command": print_command(host, "EXECUTOR_REAL_COMMAND"), "login": False, "yield_time_ms": 10000})
    assert not result["is_error"], result
    assert "EXECUTOR_REAL_COMMAND" in result["summary"]
    record = host.state.tool_calls[-1]
    commands = host.background.list_commands(conversation_id="source-owner", include_completed=True)
    assert len(commands) == 1 and commands[0]["exit_code"] == 0 and commands[0]["status"] == "completed"
    command = host.background.get_status(commands[0]["command_id"], conversation_id="source-owner")
    assert command.environment_id == host.environments.primary().selection.environment_id
    assert command.executor_remote is (host.kind == "source")
    if host.kind == "source":
        assert command.pid is None and command.executor_process_id and command.executor_session_id
    assert record.request_digest == result["request_digest"]


@pytest.mark.asyncio
async def test_real_input_and_cancel_keep_the_original_captured_process(executor_host):
    host = executor_host
    windows = host.environments.primary().peer_info["platformOs"] == "windows"
    command = "$line=[Console]::ReadLine(); Write-Output ('EXECUTOR_ECHO:'+$line); Start-Sleep -Seconds 30" if windows else "IFS= read -r line; printf 'EXECUTOR_ECHO:%s\\n' \"$line\"; sleep 30"
    _, result, _ = await batch(host, "run_command", {"command": command, "login": False, "yield_time_ms": 0})
    assert not result["is_error"], result
    item = host.background.list_commands(conversation_id="source-owner")[0]
    command_id = item["command_id"]
    actual = host.background._processes[command_id]
    assert await host.background.write_stdin(command_id, "original\n", conversation_id="source-owner", context=host.context, wait_ms=2000, cursor=0) == 9
    assert "EXECUTOR_ECHO:original" in host.background.get_output(command_id, conversation_id="source-owner")
    assert host.background._processes[command_id] is actual
    assert await host.background.cancel(command_id, conversation_id="source-owner")
    final = host.background.get_status(command_id, conversation_id="source-owner")
    assert final.status == "cancelled" and not final.cleanup_pending
    assert actual.handle.closed and actual.handle.released


@pytest.mark.asyncio
async def test_actual_start_rejection_is_not_a_successful_running_command(executor_host):
    host = executor_host
    attachment = host.environments.primary()
    policy = EnvironmentCommandPolicy(attachment.permission_scope, attachment, attachment.config.shell_environment_policy, {}, 5, True)
    command = await host.background.run_background("owned missing executable", cwd=attachment.selection.cwd,
        conversation_id="source-owner", sandbox_policy=policy, argv=["minicode-owned-program-that-does-not-exist"])
    await host.background.wait_for_startup(command.command_id, conversation_id="source-owner")
    await host.background._tasks[command.command_id]
    assert command.status == "failed" and command.pid is None and command.exit_code is None
    assert not command.cleanup_pending, command.to_dict()
    assert command.output and command.lifecycle.error["kind"] == "runtime_error"


@pytest.mark.asyncio
async def test_read_edit_checkpoint_share_actual_executor_uri_and_hash(executor_host):
    host = executor_host
    target = host.workspace / "owned-edit.txt"
    target.write_text("before\n", encoding="utf-8")
    _, read, _ = await batch(host, "read_file", {"file_path": "owned-edit.txt"})
    assert not read["is_error"], read
    _, changed, events = await batch(host, "edit_file", {"file_path": "owned-edit.txt", "old_string": "before", "new_string": "after"})
    assert not changed["is_error"], changed
    assert target.read_text(encoding="utf-8") == "after\n"
    checkpoint = next(payload for kind, payload in host.events if kind == "checkpoint.created")
    assert checkpoint["metadata"]["environment_id"] == host.environments.primary().selection.environment_id
    assert any(event.type == "tool_result" for event in events)
    target.write_text("changed outside the read snapshot\n", encoding="utf-8")
    _, stale, _ = await batch(host, "write_file", {"file_path": "owned-edit.txt", "content": "must not replace changed bytes\n"})
    assert stale["is_error"] and "changed" in stale["summary"].lower(), stale
    assert target.read_text(encoding="utf-8") == "changed outside the read snapshot\n"


@pytest.mark.asyncio
async def test_permission_request_uses_selected_uri_and_records_only_approved_subset(executor_host):
    host = executor_host
    cwd = host.environments.primary().selection.cwd
    requested = cwd + "/permission-request"
    approved = requested + "/child"

    async def approve(_call_id):
        return {"permissions": {"file_system": {"write": [approved]}}, "scope": "turn"}

    host.context = replace(host.context, permission=replace(host.context.permission, mode="confirm", approval_policy="on-request"), approval_handler=approve)
    _, result, _ = await batch(host, "request_permissions", {"permissions": {"file_system": {"write": [requested]}}})
    assert not result["is_error"], result
    environment_id = host.environments.primary().selection.environment_id
    granted = host.context.permission_grants.granted(environment_id, host.context.turn_id)
    assert permission_profile_payload(granted)["file_system"] == {"write": [approved]}
    request = next(payload for kind, payload in host.events if kind == "approval_request")
    assert request["environment_id"] == environment_id and request["cwd"] == cwd


@pytest.mark.asyncio
async def test_configured_empty_turn_never_falls_back_to_host_command(executor_host):
    host = executor_host
    host.context.run_context.turn_environments = TurnEnvironmentSnapshot()
    _, result, _ = await batch(host, "run_command", {"command": print_command(host, "MUST_NOT_EXECUTE"), "login": False})
    assert result["is_error"] and "No usable execution environment" in result["summary"], result
    assert host.background.list_commands(conversation_id="source-owner", include_completed=True) == []


@pytest.mark.asyncio
async def test_schema_selectors_follow_actual_selected_readiness_and_source_feature_gate(executor_host):
    host = executor_host
    single = policy_for_environment_snapshot(ToolsetPolicy(), host.environments, stable_environment_tools=False)
    assert single.environment_count == 1
    assert "environment_id" not in host.tool.model_schema_for_policy(single).parameters["properties"]
    host.manager.upsert({"id": "second", "program": sys.executable, "args": [str(PEER)], "cwd": str(host.workspace)})
    original = host.environments.primary().input_selection()
    pending = TurnEnvironmentSelection.from_request(TurnEnvironmentRequest("second", original.cwd,
        original.workspace_roots, EnvironmentConfigState.pending()))
    await host.owner.update_selections((original, pending))
    captured = await host.owner.snapshot()
    try:
        legacy = policy_for_environment_snapshot(ToolsetPolicy(), captured, stable_environment_tools=False)
        stable = policy_for_environment_snapshot(ToolsetPolicy(), captured, stable_environment_tools=True)
        assert legacy.environment_count == 1 and stable.environment_count == 2
        assert "environment_id" not in host.tool.model_schema_for_policy(legacy).parameters["properties"]
        assert host.tool.model_schema_for_policy(stable).parameters["properties"]["environment_id"]["type"] == "string"
        assert stable.cache_key() != legacy.cache_key()
        empty = policy_for_environment_snapshot(ToolsetPolicy(), TurnEnvironmentSnapshot(), stable_environment_tools=False)
        assert not empty.advertise_environment_tools
        assert host.registry.get_tool_schema("run_command", toolset_policy=empty) is None
    finally:
        await captured.aclose()


@pytest.mark.asyncio
async def test_owner_managed_command_restriction_uses_actual_source_requirements_parser(executor_host):
    host = executor_host
    original = host.environments.primary()
    restriction = {"prefix_rules": [{"pattern": [{"token": "git"}], "decision": "forbidden"}]}
    ready = TurnEnvironmentSelection.from_request(TurnEnvironmentRequest(original.selection.environment_id,
        original.selection.cwd, original.selection.workspace_roots,
        EnvironmentConfigState.ready(replace(original.config, exec_policy=restriction))))
    await host.owner.update_selections((ready,))
    captured = await host.owner.snapshot()
    host.context.run_context.turn_environments = captured
    try:
        _, result, _ = await batch(host, "run_command", {"command": "git --version", "login": False})
        assert result["is_error"], result
        assert "forbid" in result["summary"].lower() or "not allowed" in result["summary"].lower(), result
        assert host.background.list_commands(conversation_id="source-owner", include_completed=True) == []
    finally:
        await captured.aclose()


def test_remote_orphan_receipt_never_claims_host_pid_cleanup(tmp_path, monkeypatch):
    save_task("remote-owner", "remote-command", "retained command", "owned", "file:///remote/project", None, 1, 0,
        base_dir=tmp_path, owner_pid=999999999, owner_start_time=1, executor_remote=True,
        environment_id="target", executor_process_id="original-process", executor_session_id="original-session")
    monkeypatch.setattr("backend.terminal.task_persistence._terminate_owned_process", lambda _task: pytest.fail("remote command reached the host reaper"))
    records = cleanup_orphaned_tasks("remote-owner", tmp_path)
    assert len(records) == 1 and records[0].cleanup_pending
    restored = load_task("remote-owner", "remote-command", tmp_path)
    assert restored.status == "interrupted" and restored.cleanup_reason == "executor_session_unavailable"
    assert (restored.environment_id, restored.executor_process_id, restored.executor_session_id) == ("target", "original-process", "original-session")


def test_environment_identity_is_part_of_authorization_but_raw_arguments_stay_separate():
    call = ToolCallEvent(id="same", name="read_file", arguments={"file_path": "same.txt"})
    first = FinalExecutableToolRequest.freeze(call, environment_identity={"environment_id": "one", "registration": 1})
    second = FinalExecutableToolRequest.freeze(call, environment_identity={"environment_id": "two", "registration": 2})
    assert first.digest != second.digest and first.arguments_digest == second.arguments_digest
    assert first.matches_arguments(call.arguments) and second.matches_arguments(call.arguments)
