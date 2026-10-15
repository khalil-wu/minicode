from __future__ import annotations

import asyncio
import json
import shlex
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

from backend.artifact.store import ArtifactStore
from backend.agent.tool_execution import _authorize_final_tool_request
from backend.llm.base import ToolCallEvent
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.sandbox.policy import SandboxPolicy
from backend.sandbox.runner import SandboxCapability, SandboxRunner
from backend.terminal.manager import BackgroundCommandManager
from backend.tests.native_command_harness import configure_command_context, execute_command
from backend.tools.command_support import command_shell_argv
from backend.tools.command_tool import RunCommandTool
from backend.tools.monitor_tool import MonitorTool
from backend.tools.registry import ToolRegistry


def python_command(script: str) -> str:
    if sys.platform == "win32":
        return "& '" + sys.executable.replace("'", "''") + "' -u -c '" + script.replace("'", "''") + "'"
    return shlex.join([sys.executable, "-u", "-c", script])


def setup_tools(tmp_path: Path, *, stream_callback=None):
    manager = BackgroundCommandManager(session_id="source-shell-tests")
    context = ToolExecutionContext(permission=PermissionContext(mode="bypass"),
        workspace_root=tmp_path, conversation_id="source-shell-chat", session_id="source-shell-tests",
        background_manager=manager, stream_callback=stream_callback)
    tool = RunCommandTool(ArtifactStore(storage_dir=tmp_path / "artifacts"), manager)
    configure_command_context(tool, context, sandbox_policy=SandboxPolicy.bypass())
    registry = context.tool_registry
    return manager, tool, registry, context


async def wait_for_command_text(manager, context, command_id: str, expected: str):
    cursor = 0
    while True:
        record = await manager.wait(command_id, conversation_id=context.command_scope_id, wait_ms=10000, cursor=cursor)
        if expected in record.output:
            return record
        assert record.status == "running", record.output
        cursor = record.output_bytes


def test_source_shell_parameters_reach_execution_schema_and_reject_wrong_type(tmp_path):
    async def scenario():
        manager, tool, registry, context = setup_tools(tmp_path)
        try:
            assert {"shell", "login", "tty"} <= tool.get_execution_schema().parameters["properties"].keys()
            result = await registry.execute("run_command", {"command": "echo untouched", "tty": "true"}, context=context)
            assert result.is_error and result.error_kind == "validation_error"
            assert manager.list_commands(include_completed=True, conversation_id=context.command_scope_id) == []
        finally:
            await manager.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize("shell, flag", [("cmd", "/c"), ("powershell", "-Command"), ("pwsh", "-Command")])
def test_source_model_path_selects_shell_type_and_not_an_arbitrary_binary(tmp_path, monkeypatch, shell, flag):
    for name in ("cmd", "pwsh"):
        (tmp_path / f"{name}.exe").write_bytes(b"not executed")
    monkeypatch.setattr("backend.tools.command_support.shutil.which", lambda name:
        str(tmp_path / f"{name}.exe") if name in {"cmd", "pwsh"} else None)
    argv = command_shell_argv("echo source", shell=f"untrusted-directory/{shell}.exe", login=False)
    assert flag in argv
    assert "untrusted-directory" not in argv[0]
    if flag == "-Command":
        assert "-NoProfile" in argv
        assert "-NoProfile" not in command_shell_argv("echo source", shell=shell, login=True)


@pytest.mark.parametrize("login", [False, True])
def test_real_selected_shell_receives_source_login_semantics(tmp_path, login):
    async def scenario():
        manager, _tool, registry, context = setup_tools(tmp_path)
        command = ("Write-Output ([Environment]::GetCommandLineArgs() -contains '-NoProfile')" if sys.platform == "win32"
                   else "if shopt -q login_shell; then printf true; else printf false; fi")
        try:
            result = await execute_command(_tool, {"command": command, "shell": "pwsh" if sys.platform == "win32" else "bash", "login": login,
                "yield_time_ms": 10000}, context=context)
            assert not result.is_error, result.content
            captured = manager.get_output_chunk(result.runtime_metadata["command_id"],
                conversation_id=context.command_scope_id, cursor=0, max_chars=10000)[0]
            assert captured.strip() == (str(not login) if sys.platform == "win32" else str(login).lower())
            assert result.runtime_metadata["exit_code"] == 0
        finally:
            await manager.shutdown()
    asyncio.run(scenario())


def test_managed_login_disable_rejects_explicit_true_before_launch(tmp_path, monkeypatch):
    from backend.config_layers import ConfigLayer, ConfigLayerSource, ConfigLayerStack
    from backend.config_requirements import ConfigRequirements

    stack = ConfigLayerStack(layers=(ConfigLayer(ConfigLayerSource(kind="user"), {"allow_login_shell": True}),),
        requirements=ConfigRequirements(raw={"allow_login_shell": False}))
    monkeypatch.setattr("backend.config.load_config_layer_stack", lambda **_kwargs: stack)
    async def scenario():
        manager, _tool, registry, context = setup_tools(tmp_path)
        from backend.permissions.exec_policy import ExecPolicyOwner
        context.run_context.exec_policy_owner = ExecPolicyOwner(home=tmp_path / ".managed-command-policy", stack=stack)
        try:
            result = _authorize_final_tool_request(ToolCallEvent(id="managed-login", name="run_command",
                arguments={"command": "echo untouched", "login": True}), tool_registry=registry,
                permission_checker=context.permission_checker, permission_context=context.permission, tool_ctx=context)
            assert "login shell is disabled" in result.error
            assert manager.list_commands(include_completed=True, conversation_id=context.command_scope_id) == []
            omitted = command_shell_argv("echo source", shell="pwsh" if sys.platform == "win32" else "bash", allow_login_shell=False)
            assert ("-NoProfile" if sys.platform == "win32" else "-c") in omitted
        finally:
            await manager.shutdown()
    asyncio.run(scenario())


def test_real_cmd_shell_keeps_exit_code_and_public_command(tmp_path):
    async def scenario():
        manager, _tool, registry, context = setup_tools(tmp_path)
        command = "echo SOURCE_CMD&exit /b 7" if sys.platform == "win32" else "printf SOURCE_CMD; exit 7"
        try:
            result = await execute_command(_tool, {"command": command, "shell": "cmd" if sys.platform == "win32" else "sh", "login": False,
                "yield_time_ms": 10000}, context=context)
            assert not result.is_error and result.status == "success"
            assert result.runtime_metadata["process_status"] == "failed"
            assert result.runtime_metadata["exit_code"] == 7
            record = manager.get_status(result.runtime_metadata["command_id"], conversation_id=context.command_scope_id)
            assert record.command == command and not record.tty
            assert "SOURCE_CMD" in record.output
            assert "--run-as-pty" not in record.argv
        finally:
            await manager.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize("tty", [False, True])
def test_real_pipe_and_pty_share_owner_yield_stdin_and_durable_output(tmp_path, tty):
    async def scenario():
        manager, _tool, registry, context = setup_tools(tmp_path)
        script = "import sys; print('READY:'+str(sys.stdin.isatty())+':'+str(sys.stdout.isatty()),flush=True); line=sys.stdin.readline(); print('SOURCE_ECHO:'+line.strip(),flush=True)"
        command = python_command(script)
        try:
            result = await execute_command(_tool, {"command": command, "login": False, "tty": tty,
                "yield_time_ms": 0}, context=context)
            assert not result.is_error, result.content
            command_id = result.runtime_metadata["command_id"]
            record = await asyncio.wait_for(wait_for_command_text(manager, context, command_id, "READY:"), timeout=15)
            assert record.command == command and record.tty is tty
            assert f"READY:{tty}:{tty}" in record.output
            process_id = record.pid
            foreign = replace(context, conversation_id="foreign-chat")
            denied = await MonitorTool().execute({"action": "write_stdin", "command_id": command_id, "chars": "WRONG\n"}, foreign)
            assert denied.is_error and "WRONG" not in record.output
            echoed = await MonitorTool().execute({"action": "write_stdin", "command_id": command_id,
                "chars": "你好 source\n", "cursor": 0, "yield_time_ms": 10000}, context)
            assert not echoed.is_error, echoed.content
            await manager.wait(command_id, conversation_id=context.command_scope_id, wait_ms=10000)
            assert record.status == "completed" and record.pid == process_id and record.exit_code == 0
            raw = manager.get_output_chunk(command_id, conversation_id=context.command_scope_id, cursor=0, max_chars=10000)[0]
            assert "SOURCE_ECHO:你好 source" in raw
            assert Path(record.output_path).read_bytes().decode("utf-8") == raw
            assert not record.cleanup_pending
        finally:
            await manager.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize("terminal_action", ["timeout", "cancel"])
def test_real_pty_timeout_and_cancel_settle_the_owned_tree(tmp_path, terminal_action):
    async def scenario():
        manager, _tool, registry, context = setup_tools(tmp_path)
        command = python_command("import time; print('SOURCE_READY',flush=True); time.sleep(60)")
        try:
            result = await execute_command(_tool, {"command": command, "tty": True, "login": False,
                "timeout": 3 if terminal_action == "timeout" else 30, "yield_time_ms": 0}, context=context)
            assert not result.is_error, result.content
            command_id = result.runtime_metadata["command_id"]
            record = await asyncio.wait_for(wait_for_command_text(manager, context, command_id, "SOURCE_READY"), timeout=15)
            process_id = record.pid
            children = psutil.Process(process_id).children(recursive=True)
            child_evidence = [(child.pid, child.name(), child.create_time()) for child in children]
            if terminal_action == "cancel":
                await MonitorTool().execute({"action": "cancel", "command_id": command_id}, context)
            await manager.wait(command_id, conversation_id=context.command_scope_id, wait_ms=10000)
            assert record.status == ("failed" if terminal_action == "timeout" else "cancelled")
            assert not record.cleanup_pending
            assert not psutil.pid_exists(process_id)
            assert all(not child.is_running() for child in children), [evidence for child, evidence in zip(children, child_evidence, strict=True) if child.is_running()]
            assert "SOURCE_READY" in Path(record.output_path).read_text(encoding="utf-8")
            if terminal_action == "timeout":
                assert record.lifecycle.error["kind"] == "timeout"
        finally:
            await manager.shutdown()
    asyncio.run(scenario())


def test_tty_cannot_escape_an_unavailable_required_sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(SandboxRunner, "capability", lambda self, **_kwargs:
        SandboxCapability(backend="windows-elevated-wfp", available=False, filesystem_isolated=False,
                          network_isolated=False, reason="required policy cannot launch"))
    async def scenario():
        manager, _tool, registry, context = setup_tools(tmp_path)
        context.permission = PermissionContext(mode="confirm")
        context.sandbox_policy = SandboxPolicy(workspace_root=tmp_path)
        try:
            result = await execute_command(_tool, {"command": "echo untouched", "tty": True}, context=context)
            assert result.is_error and "required policy cannot launch" in result.content
            assert manager.list_commands(include_completed=True, conversation_id=context.command_scope_id) == []
        finally:
            await manager.shutdown()
    asyncio.run(scenario())


def test_native_sandbox_tty_keeps_exact_argv_and_permission_profile(tmp_path, monkeypatch):
    from backend.sandbox import windows_native

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    desktop = SimpleNamespace(name="private-test-desktop", close=lambda: None)
    runtime = windows_native.WindowsNativeRuntime(tmp_path / "codex.exe", tmp_path / "sandbox-home",
        "offline-fixture", "online-fixture", "owner", "sid", "group")
    monkeypatch.setattr(windows_native, "discover_runtime", lambda: (runtime, ""))
    monkeypatch.setattr(windows_native.PrivateDesktop, "create", lambda _account: desktop)
    monkeypatch.setattr(windows_native, "STATE_ROOT", tmp_path / "state")
    argv = command_shell_argv("Write-Output source", shell="pwsh", login=False)
    policy = SandboxPolicy(workspace_root=workspace, allow_network=False)
    prepared, actual_desktop, private_temp = windows_native.prepare_command("Write-Output source", cwd=workspace,
        resolved=policy.resolve(cwd=workspace), env={"PATH": "fixture"}, workspace_roots=(workspace,),
        deny_read_paths=(workspace / "private",), deny_write_paths=(workspace / ".git",), argv=argv, tty=True)
    assert "--tty" in prepared
    assert prepared[prepared.index("--") + 1:] == argv
    assert json.loads(prepared[prepared.index("--permission-profile") + 1])["network"] == "restricted"
    assert json.loads(prepared[prepared.index("--deny-read-paths-json") + 1]) == [str(workspace / "private")]
    assert actual_desktop is desktop and private_temp.is_dir()
    private_temp.rmdir()
