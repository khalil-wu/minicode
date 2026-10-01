import pytest

from backend.permissions.checker import check_catastrophic_command


@pytest.mark.parametrize("command", [
    "bash -c 'rm -rf /'",
    'sh -c "bash -c \'rm -rf /\'"',
    "pwsh -Command 'rm -rf /'",
])
def test_catastrophic_shell_wrappers_are_unwrapped(command: str) -> None:
    allowed, reason = check_catastrophic_command(command)
    assert allowed is False
    assert reason


@pytest.mark.parametrize("command", [
    "git log && rm -rf build",
    "git status; git clean -fdx",
    "Get-ChildItem; Remove-Item -Recurse build",
    "echo ok; git checkout .",
    "echo ok; git stash clear",
])
def test_destructive_compound_commands_are_blocked(command: str) -> None:
    allowed, reason = check_catastrophic_command(command)
    assert allowed is False
    assert "compound command" in reason


def test_quoted_separator_does_not_create_a_false_compound_command() -> None:
    assert check_catastrophic_command("echo 'git log && rm -rf build'") == (True, "")


def test_single_project_cleanup_still_uses_normal_approval_policy() -> None:
    assert check_catastrophic_command("rm -rf build") == (True, "")


@pytest.mark.parametrize("command", [
    "Stop-Process -Name python -ErrorAction SilentlyContinue",
    "Get-Process | Stop-Process -Force",
    "Get-Process | ForEach-Object -MemberName Kill",
    "Get-Process | ForEach-Object Kill",
    "Get-Process | ForEach-Object { $_.Kill() }",
    "taskkill /F /IM python.exe",
    "pkill -f uvicorn",
    "killall python",
    "Get-CimInstance Win32_Process | Invoke-CimMethod -MethodName Terminate",
    "Get-WmiObject Win32_Process | ForEach-Object { $_.Terminate() }",
    "wmic process call terminate",
    "pwsh -Command 'Stop-Process -Name python'",
])
def test_broad_process_termination_is_blocked(command: str) -> None:
    allowed, reason = check_catastrophic_command(command)
    assert allowed is False
    assert "owned background command" in reason


@pytest.mark.parametrize("command", [
    "Stop-Process -Id 1234 -Force",
    "taskkill /PID 1234 /T /F",
    "kill -TERM 1234",
])
def test_exact_pid_termination_keeps_normal_approval_policy(command: str) -> None:
    assert check_catastrophic_command(command) == (True, "")


@pytest.mark.parametrize("command", [
    "$(rm -rf /)",
    "`rm -rf /`",
    "echo $(rm -rf /)",
    "bash -c 'echo `rm -rf /`'",
])
def test_command_substitutions_cannot_hide_catastrophic_commands(command: str) -> None:
    allowed, reason = check_catastrophic_command(command)
    assert allowed is False
    assert "root filesystem" in reason


def test_windows_drive_recursive_delete_is_catastrophic() -> None:
    allowed, reason = check_catastrophic_command("del /s C:\\")
    assert allowed is False
    assert "drive root" in reason


@pytest.mark.parametrize("command", [
    "git clean -fdx",
    "git reset --hard",
    "git checkout .",
    "git checkout -- .",
    "git restore .",
    "git stash drop",
    "git stash clear",
    "git branch -D feature",
    "git push --force origin main",
    "git commit --amend",
    "kubectl delete pod api",
    "terraform destroy",
    "DROP TABLE users",
])
def test_destructive_operations_are_explicit_confirmation_boundaries(command: str) -> None:
    allowed, reason = check_catastrophic_command(command)
    assert allowed is False
    assert reason


def test_runtime_review_posix_clusters_bind_exact_payload():
    import asyncio
    import shlex
    from pathlib import Path
    from unittest.mock import AsyncMock
    from backend.agent.final_tool_request import canonical_tool_request_digest
    from backend.config import PermissionSettings
    from backend.permissions.checker import (
        PermissionChecker, _shell_wrapper_payload, check_catastrophic_command,
        evaluate_permission_decision,
    )
    from backend.permissions.context import PermissionContext, ToolExecutionContext
    from backend.permissions.shell_ast import parse_literal_commands, posix_shell_payload_index
    from backend.tools.base import ToolResult
    from backend.tools.command_tool import RunCommandTool

    async def verify():
        tool = RunCommandTool(None)
        dispatch = AsyncMock(return_value=ToolResult(content="INTERCEPTED_BEFORE_SPAWN"))
        tool._execute_foreground = dispatch
        checker = PermissionChecker(PermissionSettings(), workspace_root=Path.cwd())
        context = ToolExecutionContext(
            permission=PermissionContext(mode="bypass"), workspace_root=Path.cwd(),
            permission_checker=checker,
        )
        payload = "'r''m' -rf /"
        for invocation in ("bash -xec", "bash -exc", "bash +c", "bash -c -x",
                           "bash -ec --", "bash -e -o pipefail -c", "bash -o -c",
                           "bash --rcfile startup.rc -xec", "bash.exe -xec"):
            command = f'{invocation} "{payload}"'
            argv = shlex.split(command)
            assert argv[posix_shell_payload_index(argv)] == payload
            assert _shell_wrapper_payload(command) == (payload, "")
            parsed = parse_literal_commands(command)
            assert parsed is not None and ["rm", "-rf", "/"] in parsed.commands
            assert check_catastrophic_command(command)[0] is False
            args = {"command": command}
            decision = evaluate_permission_decision(checker, tool.name, args, context=context.permission, tool=tool)
            assert decision.decision == "ask"
            context.metadata.clear()
            result = await tool.execute(args, context)
            assert result.is_error and "explicit approval" in result.content
            dispatch.assert_not_awaited()
            context.metadata["_approved_request_digests"] = {canonical_tool_request_digest(tool.name, args)}
            result = await tool.execute(args, context)
            assert result.content == "INTERCEPTED_BEFORE_SPAWN"
            dispatch.assert_awaited_once()
            dispatch.reset_mock()

        assert posix_shell_payload_index(["bash", "script.sh", "-c", payload]) is None
        assert posix_shell_payload_index(["bash", "--rcfile", "-c", "script.sh"]) is None
        assert _shell_wrapper_payload("bash -xec") == (None, "shell wrapper is missing its command payload")
        assert check_catastrophic_command("bash -xec 'printf harmless'")[0] is True

    asyncio.run(verify())
