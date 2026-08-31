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
