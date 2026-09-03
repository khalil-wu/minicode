"""Tests for sandbox execution, protected paths, and network isolation."""
from __future__ import annotations

import asyncio
import json
import shlex
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from backend.sandbox import SandboxPolicy, SandboxResult, SandboxRunner
from backend.sandbox.policy import (
    FileSystemAccessMode,
    FileSystemPath,
    FileSystemSandboxEntry,
    FileSystemSandboxPolicy,
    FileSystemSpecialPath,
    PermissionProfile,
)
from backend.sandbox.runner import (
    _seatbelt_profile,
    _seatbelt_policy_supported,
    _seatbelt_regex_for_unreadable_glob,
)


# ── SandboxPolicy tests ──


def test_workspace_default_policy_denies_network(tmp_path: Path) -> None:
    policy = SandboxPolicy.workspace_default(tmp_path)
    assert not policy.allow_network
    assert tmp_path in policy.writable_roots


def test_permissive_policy_allows_network(tmp_path: Path) -> None:
    policy = SandboxPolicy.permissive(tmp_path)
    assert policy.allow_network
    assert tmp_path in policy.writable_roots


# ── SandboxRunner tests ──


def _python_command(script: str) -> str:
    parts = [sys.executable, "-c", script]
    if sys.platform == "win32":
        return subprocess.list2cmdline(parts)
    return " ".join(shlex.quote(part) for part in parts)


def test_execution_entrypoint_sandbox_coverage_is_explicit() -> None:
    command_tool = Path("backend/tools/command_tool.py").read_text(encoding="utf-8")
    background_manager = Path("backend/terminal/manager.py").read_text(encoding="utf-8")
    sandbox_runner = Path("backend/sandbox/runner.py").read_text(encoding="utf-8")
    terminal_session = Path("backend/terminal/session.py").read_text(encoding="utf-8")
    mcp_client = Path("backend/mcp/client.py").read_text(encoding="utf-8")
    hook_runners = Path("backend/hooks/runners.py").read_text(encoding="utf-8")

    assert "SandboxRunner(policy)" in command_tool
    assert '"sandbox_policy": policy' in command_tool
    assert "runner.run(" in background_manager
    assert "host_command=host_command" in background_manager
    assert "shell_subprocess_env(" in sandbox_runner
    assert "sanitized_subprocess_env" in terminal_session
    assert "mcp_subprocess_env(self._env)" in mcp_client
    assert "sanitized_subprocess_env()" in hook_runners
    assert "event_policy(event).default_timeout_seconds" in hook_runners


def test_sandbox_runner_executes_simple_command(tmp_path: Path) -> None:
    policy = SandboxPolicy.bypass(timeout=10)
    runner = SandboxRunner(policy)
    cmd = "echo hello" if sys.platform != "win32" else "cmd /c echo hello"
    result = asyncio.run(runner.run(cmd, cwd=tmp_path))
    assert result.exit_code == 0
    assert "hello" in result.stdout


def test_sandbox_runner_captures_stderr(tmp_path: Path) -> None:
    policy = SandboxPolicy.bypass(timeout=10)
    runner = SandboxRunner(policy)
    if sys.platform == "win32":
        cmd = "cmd /c echo err 1>&2"
    else:
        cmd = "echo err >&2"
    result = asyncio.run(runner.run(cmd, cwd=tmp_path))
    assert "err" in result.stderr


def test_sandbox_runner_stream_callback_labels_stdout_and_stderr(tmp_path: Path) -> None:
    policy = SandboxPolicy.bypass(timeout=10)
    runner = SandboxRunner(policy)
    streamed: list[tuple[str, str]] = []

    async def _run() -> SandboxResult:
        async def _stream(piece: str, stream: str) -> None:
            if piece:
                streamed.append((stream, piece))

        return await runner.run(
            _python_command(
                "import sys; sys.stdout.write('out\\n'); sys.stdout.flush(); "
                "sys.stderr.write('err\\n'); sys.stderr.flush()"
            ),
            cwd=tmp_path,
            stream_callback=_stream,
        )

    result = asyncio.run(_run())

    assert result.exit_code == 0
    assert any(stream == "stdout" and "out" in piece for stream, piece in streamed)
    assert any(stream == "stderr" and "err" in piece for stream, piece in streamed)


def test_sandbox_stream_callback_type_error_is_not_retried(tmp_path: Path) -> None:
    runner = SandboxRunner(SandboxPolicy.bypass(timeout=10))
    calls = 0

    async def _stream(*_args: object) -> None:
        nonlocal calls
        calls += 1
        raise TypeError("callback body failed")

    result = asyncio.run(
        runner.run(
            _python_command("print('one')"),
            cwd=tmp_path,
            stream_callback=_stream,
        )
    )

    assert result.exit_code == 0
    assert calls == 1


def test_sandbox_runner_timeout(tmp_path: Path) -> None:
    policy = SandboxPolicy.bypass(timeout=1)
    runner = SandboxRunner(policy)
    if sys.platform == "win32":
        cmd = "ping -n 30 127.0.0.1"
    else:
        cmd = "sleep 30"
    result = asyncio.run(runner.run(cmd, cwd=tmp_path))
    assert result.timed_out
    assert result.exit_code == -1


def test_sandbox_runner_cancel(tmp_path: Path) -> None:
    policy = SandboxPolicy.bypass(timeout=30)
    runner = SandboxRunner(policy)
    cancel = asyncio.Event()

    async def _run_with_cancel():
        async def _cancel_after_delay():
            await asyncio.sleep(0.3)
            cancel.set()

        asyncio.create_task(_cancel_after_delay())
        if sys.platform == "win32":
            cmd = "ping -n 30 127.0.0.1"
        else:
            cmd = "sleep 30"
        return await runner.run(cmd, cwd=tmp_path, cancel_event=cancel)

    result = asyncio.run(_run_with_cancel())
    assert result.cancelled


def test_sandbox_runner_nonexistent_command(tmp_path: Path) -> None:
    policy = SandboxPolicy.bypass(timeout=5)
    runner = SandboxRunner(policy)
    result = asyncio.run(runner.run("__nonexistent_cmd_xyz__", cwd=tmp_path))
    assert result.exit_code != 0


# ── Network isolation tests ──


def test_sandbox_network_denied_by_default(tmp_path: Path) -> None:
    policy = SandboxPolicy.workspace_default(tmp_path, timeout=10)
    runner = SandboxRunner(policy)
    if not runner.capability().available:
        pytest.skip(runner.capability().reason)
    result = asyncio.run(runner.run("curl -s --max-time 3 http://1.1.1.1", cwd=tmp_path))
    assert result.exit_code != 0


# ── Protected paths tests ──


def test_protected_write_path_detection() -> None:
    from backend.security.sensitive_files import is_protected_write_path

    assert is_protected_write_path(Path(".git/hooks/pre-commit"))
    assert is_protected_write_path(Path(".git/config"))
    assert is_protected_write_path(Path("project/.git/objects/abc"))
    assert is_protected_write_path(Path(".mcp.json"))
    assert is_protected_write_path(Path(".minicode/settings.json"))
    assert is_protected_write_path(Path(".minicode/memory/note.md"))
    assert is_protected_write_path(Path(".bashrc"))
    # Everything under MiniCode's own state directory is protected, because that
    # is where its instructions, rules, agents and checkpoints live.
    assert is_protected_write_path(Path(".minicode/config.json"))
    # A bare settings.json outside .minicode belongs to the user's project, and
    # credential files are governed by the approval flow rather than a refusal.
    assert not is_protected_write_path(Path("settings.json"))
    assert not is_protected_write_path(Path(".env"))
    assert not is_protected_write_path(Path("src/main.py"))
    assert not is_protected_write_path(Path("docs/guide.md"))


def test_protected_write_path_blocks_gitconfig() -> None:
    from backend.security.sensitive_files import is_protected_write_path

    assert is_protected_write_path(Path(".gitconfig"))
    assert is_protected_write_path(Path(".gitmodules"))


# ── Command reads outside workspace fail ──


def test_command_cwd_outside_workspace_rejected(tmp_path: Path) -> None:
    from backend.tools.command_tool import RunCommandTool
    from backend.artifact.store import ArtifactStore

    class FakeContext:
        workspace_root = str(tmp_path / "workspace")
        cancel_event = None
        stream_callback = None
        allow_network = False

    (tmp_path / "workspace").mkdir()
    tool = RunCommandTool(ArtifactStore(storage_dir=tmp_path / "artifacts"))
    result = asyncio.run(
        tool.execute(
            {"command": "echo hi", "cwd": str(tmp_path / "outside")},
            context=FakeContext(),
        )
    )
    assert result.is_error
    assert "workspace" in result.content.lower()


# ── Filesystem boundary tests ──


def test_sandbox_blocks_write_outside_writable_roots(tmp_path: Path) -> None:
    """Verify that Seatbelt/Bubblewrap blocks writes outside writable_roots."""

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    policy = SandboxPolicy(
        permission_profile=PermissionProfile.managed(
            FileSystemSandboxPolicy.restricted(
                [
                    FileSystemSandboxEntry(
                        FileSystemPath.special(FileSystemSpecialPath.MINIMAL),
                        FileSystemAccessMode.READ,
                    ),
                    FileSystemSandboxEntry(
                        FileSystemPath.path(workspace),
                        FileSystemAccessMode.WRITE,
                    ),
                ]
            )
        ),
        workspace_root=workspace,
        timeout=10,
    )
    runner = SandboxRunner(policy)
    if not runner.capability().available:
        pytest.skip(runner.capability().reason)

    # Write inside workspace should succeed
    if sys.platform == "win32":
        from backend.tools.command_support import _windows_powershell_shell_command
        inside_path = str(workspace / "test_file.txt").replace("'", "''")
        outside_path = str(outside / "escape.txt").replace("'", "''")
        inside_command = _windows_powershell_shell_command(
            f"Set-Content -LiteralPath '{inside_path}' -Value 'ok'",
            cwd=workspace,
        )
        outside_command = _windows_powershell_shell_command(
            f"Set-Content -LiteralPath '{outside_path}' -Value 'denied'",
            cwd=workspace,
        )
    else:
        inside_command = f"touch {workspace}/test_file.txt"
        outside_command = f"touch {outside}/escape.txt"
    result = asyncio.run(runner.run(
        inside_command,
        cwd=workspace,
        host_command=inside_command if sys.platform == "win32" else "",
    ))
    if result.sandbox_unavailable:
        pytest.skip(result.stderr)
    assert result.exit_code == 0
    assert (workspace / "test_file.txt").exists()

    # Write outside workspace should fail
    result = asyncio.run(runner.run(
        outside_command,
        cwd=workspace,
        host_command=outside_command if sys.platform == "win32" else "",
    ))
    assert result.exit_code != 0 or not (outside / "escape.txt").exists()


def test_sandbox_allows_read_from_system_paths(tmp_path: Path) -> None:
    """Verify sandbox allows reading system paths (needed for interpreters)."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    policy = SandboxPolicy(writable_roots=(workspace,), allow_network=True, timeout=10)
    runner = SandboxRunner(policy)
    if not runner.capability().available:
        pytest.skip(runner.capability().reason)

    # Reading a standard system file should work.
    if sys.platform == "win32":
        from backend.tools.command_support import _windows_powershell_shell_command
        command = _windows_powershell_shell_command(
            "Get-Content -LiteralPath (Join-Path $env:WINDIR 'win.ini')",
            cwd=workspace,
        )
    else:
        command = "ls /usr/bin/env"
    result = asyncio.run(runner.run(
        command,
        cwd=workspace,
        host_command=command if sys.platform == "win32" else "",
    ))
    if result.sandbox_unavailable:
        pytest.skip(result.stderr)
    assert result.exit_code == 0


def test_unavailable_restricted_sandbox_fails_before_process_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.sandbox.runner as runner_module

    monkeypatch.setattr(
        runner_module,
        "_container_runtime",
        lambda: ("", "", "test runtime unavailable"),
    )
    monkeypatch.setattr(
        runner_module,
        "_bubblewrap_capability",
        lambda: (False, "test bubblewrap unavailable"),
    )
    runner = SandboxRunner(SandboxPolicy.workspace_default(tmp_path))

    result = asyncio.run(runner.run("echo must-not-run", cwd=tmp_path))

    assert result.sandbox_unavailable is True
    assert result.exit_code == 126
    assert "Sandbox unavailable" in result.stderr


def test_container_wrapper_keeps_untrusted_values_out_of_the_host_shell(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.sandbox.runner as runner_module

    workspace = tmp_path / "workspace&echo.HOST_PATH"
    workspace.mkdir()
    policy = SandboxPolicy(
        workspace_root=workspace,
        writable_roots=(workspace,),
        allow_network=True,
        env_overrides={"MINICODE_PROBE": "value&echo.HOST_ENV"},
    )
    runner = SandboxRunner(policy)
    monkeypatch.setattr(
        runner_module,
        "_container_runtime",
        lambda: ("docker", "minicode-agent-sandbox:latest", ""),
    )

    wrapped = runner._container_command(
        "whoami&echo.HOST_COMMAND",
        "docker",
        cwd=workspace,
        resolved=policy.resolve(cwd=workspace),
    )

    assert wrapped[:2] == ["docker", "run"]
    assert f"--volume={workspace.resolve()}:/workspace:rw" in wrapped
    assert "--env=MINICODE_PROBE=value&echo.HOST_ENV" in wrapped
    assert wrapped[-1] == "whoami&echo.HOST_COMMAND"


def test_workspace_and_writable_roots_are_distinct_policy_fields(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    source = workspace / "src"
    source.mkdir(parents=True)

    policy = SandboxPolicy(workspace_root=workspace, writable_roots=(source,))

    assert policy.workspace_root == workspace
    assert policy.writable_roots == (source,)



def test_seatbelt_profile_escapes_path_literals() -> None:
    workspace = Path('/tmp/project")) (allow network*) (subpath "x')
    profile = _seatbelt_profile(
        SandboxPolicy(workspace_root=workspace, writable_roots=(workspace,))
    )

    assert "\\\")) (allow network*) (subpath \\\"" in profile
    assert not any(line == "(allow network*)" for line in profile.splitlines())


def test_seatbelt_unreadable_globs_use_anchored_component_regex(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    pattern = str(workspace / "**" / "*.env")
    policy = SandboxPolicy(
        workspace_root=workspace,
        permission_profile=PermissionProfile.managed(
            FileSystemSandboxPolicy.restricted(
                (
                    FileSystemSandboxEntry(
                        FileSystemPath.special(FileSystemSpecialPath.ROOT),
                        FileSystemAccessMode.READ,
                    ),
                    FileSystemSandboxEntry(
                        FileSystemPath.glob(pattern),
                        FileSystemAccessMode.DENY,
                    ),
                )
            )
        ),
    )
    resolved = policy.resolve(cwd=workspace)
    regex = _seatbelt_regex_for_unreadable_glob(pattern)
    profile = _seatbelt_profile(resolved)

    assert _seatbelt_regex_for_unreadable_glob("/tmp/repo/**/*.env") == (
        r"^/tmp/repo/(.*/)?[^/]*\.env$"
    )
    assert f'(deny file-read* (regex #"{regex}"))' in profile
    assert f'(deny file-write-unlink (regex #"{regex}"))' in profile
    assert _seatbelt_policy_supported(resolved) is True
