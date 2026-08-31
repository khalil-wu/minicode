from __future__ import annotations

import subprocess
import sys

import pytest

from backend.evals.minicode_driver import _should_emit_trace_event, _trace_event_data
from backend.sandbox.runner import (
    MAX_OUTPUT_LENGTH,
    _append_bounded_output,
    _decode_bounded_output,
)
from backend.tools.command_support import _windows_powershell_shell_command


def test_bounded_command_output_retains_beginning_and_end() -> None:
    payload = b"BEGIN\n" + (b"middle-output\n" * 4_000) + b"END\n"
    sink = bytearray()
    for offset in range(0, len(payload), 4_096):
        _append_bounded_output(sink, payload[offset : offset + 4_096])

    captured = _decode_bounded_output(sink, len(payload))

    assert captured.startswith("BEGIN\n")
    assert captured.endswith("END\n")
    assert "bytes truncated; showing beginning and end" in captured
    assert len(captured) <= MAX_OUTPUT_LENGTH


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell wrapper is Windows-specific")
@pytest.mark.parametrize("native_exit", [0, 7])
def test_windows_powershell_redirect_preserves_native_exit(native_exit: int) -> None:
    script = (
        "import sys; "
        "print('stdout marker'); "
        "print('stderr marker', file=sys.stderr); "
        f"raise SystemExit({native_exit})"
    )
    native_command = f"{subprocess.list2cmdline([sys.executable, '-c', script])} 2>&1"
    wrapped = _windows_powershell_shell_command(native_command)

    completed = subprocess.run(
        wrapped,
        shell=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    assert completed.returncode == native_exit
    assert "stdout marker" in completed.stdout
    assert "stderr marker" in (completed.stdout + completed.stderr)


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell wrapper is Windows-specific")
@pytest.mark.parametrize(
    ("command", "expected_exit"),
    [("Write-Output 'ok'", 0), ("Get-Item -LiteralPath 'Z:\\\\minicode-missing-path'", 1)],
)
def test_windows_powershell_wrapper_preserves_cmdlet_status(command: str, expected_exit: int) -> None:
    completed = subprocess.run(
        _windows_powershell_shell_command(command),
        shell=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == expected_exit


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell wrapper is Windows-specific")
def test_windows_powershell_wrapper_preserves_cwd_env_and_nested_quotes(tmp_path) -> None:
    script = (
        "import os; "
        "print(os.getcwd()); "
        "print(os.environ['MINICODE_SHELL_CONTRACT']); "
        "print('stderr marker', file=__import__('sys').stderr)"
    )
    command = (
        "$env:MINICODE_SHELL_CONTRACT='works'; $env:PYTHONIOENCODING='utf-8'; "
        f"{subprocess.list2cmdline([sys.executable, '-c', script])} 2>&1"
    )

    completed = subprocess.run(
        _windows_powershell_shell_command(command),
        cwd=tmp_path,
        shell=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    assert completed.returncode == 0
    assert str(tmp_path) in completed.stdout
    assert "works" in completed.stdout
    assert "stderr marker" in (completed.stdout + completed.stderr)


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell wrapper is Windows-specific")
def test_windows_powershell_wrapper_suppresses_serialized_progress_noise() -> None:
    completed = subprocess.run(
        _windows_powershell_shell_command(
            "Write-Progress -Activity 'download' -Status 'working'; Write-Output 'done'"
        ),
        shell=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    assert completed.returncode == 0
    assert "done" in completed.stdout
    assert "CLIXML" not in completed.stderr
    assert "System.Management.Automation.PSCustomObject" not in completed.stderr


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell wrapper is Windows-specific")
def test_windows_powershell_wrapper_reports_error_records_as_plain_text() -> None:
    # A redirected PowerShell stderr carries CLIXML-serialized error records
    # unless the host is told to emit text, so the model would otherwise read
    # `#< CLIXML <Objs ...>` with `_x001B_` escapes instead of the message.
    completed = subprocess.run(
        _windows_powershell_shell_command("Write-Error 'boom'; Write-Output 'after'"),
        shell=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    assert "after" in completed.stdout
    assert "CLIXML" not in completed.stderr
    assert "_x001B_" not in completed.stderr
    assert "\x1b[" not in completed.stderr
    assert "boom" in completed.stderr


def test_eval_trace_compacts_duplicate_tool_payloads_and_stream_noise() -> None:
    compacted = _trace_event_data(
        "tool_result",
        {
            "status": "success",
            "summary": "start-" + ("x" * 4_000) + "-end",
            "outcome": {"content": "duplicate raw artifact"},
        },
    )

    assert "outcome" not in compacted
    assert len(str(compacted["summary"])) < 2_100
    assert str(compacted["summary"]).startswith("start-")
    assert str(compacted["summary"]).endswith("-end")
    assert not _should_emit_trace_event("agent_message.delta", {})
    assert _should_emit_trace_event("done", {})
