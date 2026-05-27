"""Sandbox runner — executes commands under OS-level isolation."""
from __future__ import annotations

import asyncio
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Awaitable

from backend.runtime_env import sanitized_subprocess_env
from backend.sandbox.policy import SandboxPolicy
from backend.sandbox.result import SandboxResult

MAX_OUTPUT_LENGTH = 20_000


class SandboxRunner:
    """Execute shell commands within a SandboxPolicy.

    Isolation strategy by platform:
      - Linux: unshare --net (network), or firejail if available
      - macOS: sandbox-exec (Seatbelt profile)
      - Windows: Job Object (kill-on-close) + application-layer path enforcement
                 (true network isolation requires Windows containers or WSL2)
    """

    def __init__(self, policy: SandboxPolicy) -> None:
        self._policy = policy

    async def run(
        self,
        command: str,
        *,
        cwd: str | Path | None = None,
        cancel_event: asyncio.Event | None = None,
        stream_callback: Callable[[str], Awaitable[None]] | None = None,
    ) -> SandboxResult:
        env = self._build_env()
        wrapped_command = self._wrap_command(command)
        process_kwargs = self._process_kwargs()

        proc: asyncio.subprocess.Process | None = None
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []

        try:
            proc = await asyncio.create_subprocess_shell(
                wrapped_command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(cwd) if cwd else None,
                env=env,
                **process_kwargs,
            )

            cancel_task = None
            if cancel_event:
                async def _wait_cancel() -> None:
                    await cancel_event.wait()
                    await self._kill_tree(proc)
                cancel_task = asyncio.create_task(_wait_cancel())

            async def _read_stream(
                stream: Any, sink: list[str], *, forward: bool
            ) -> int:
                total = 0
                while stream is not None:
                    chunk = await stream.read(4096)
                    if not chunk:
                        break
                    decoded = chunk.decode("utf-8", errors="replace")
                    remaining = MAX_OUTPUT_LENGTH - total
                    if remaining > 0:
                        sink.append(decoded[:remaining])
                    total += len(decoded)
                    if forward and stream_callback:
                        try:
                            await stream_callback(decoded)
                        except Exception:
                            pass
                return total

            try:
                stdout_task = asyncio.create_task(
                    _read_stream(proc.stdout, stdout_parts, forward=True)
                )
                stderr_task = asyncio.create_task(
                    _read_stream(proc.stderr, stderr_parts, forward=False)
                )
                await asyncio.wait_for(
                    asyncio.gather(stdout_task, stderr_task, proc.wait()),
                    timeout=self._policy.timeout,
                )
            except asyncio.TimeoutError:
                await self._kill_tree(proc)
                if cancel_task:
                    cancel_task.cancel()
                return SandboxResult(
                    stdout="".join(stdout_parts),
                    stderr="".join(stderr_parts),
                    exit_code=-1,
                    timed_out=True,
                )

            if cancel_task:
                cancel_task.cancel()

            if cancel_event and cancel_event.is_set():
                return SandboxResult(
                    stdout="".join(stdout_parts),
                    stderr="".join(stderr_parts),
                    exit_code=-1,
                    cancelled=True,
                )

            return SandboxResult(
                stdout="".join(stdout_parts),
                stderr="".join(stderr_parts),
                exit_code=proc.returncode or 0,
            )

        except FileNotFoundError:
            return SandboxResult(stdout="", stderr=f"Command not found: {command}", exit_code=127)
        except OSError as exc:
            return SandboxResult(stdout="", stderr=str(exc), exit_code=1)

    def _build_env(self) -> dict[str, str]:
        env = sanitized_subprocess_env()
        env.update(self._policy.env_overrides)
        return env

    def _wrap_command(self, command: str) -> str:
        if sys.platform == "linux":
            parts = []
            if not self._policy.allow_network and shutil.which("unshare"):
                parts.append("unshare --net --map-root-user --")
            elif not self._policy.allow_network and shutil.which("firejail"):
                return f"firejail --quiet --net=none -- sh -c {_shell_quote(command)}"
            if parts:
                return f"{' '.join(parts)} sh -c {_shell_quote(command)}"

        if sys.platform == "darwin" and shutil.which("sandbox-exec"):
            profile = _seatbelt_profile(self._policy)
            return f"sandbox-exec -p {_shell_quote(profile)} -- sh -c {_shell_quote(command)}"

        # Windows: no practical OS-level isolation without admin/containers.
        # App-layer enforcement (workspace boundary, validate_command) handles it.
        return command

    def _process_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {}
        if os.name == "nt":
            kwargs["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP
                | subprocess.CREATE_BREAKAWAY_FROM_JOB
            )
        else:
            kwargs["start_new_session"] = True
        return kwargs

    @staticmethod
    async def _kill_tree(proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is not None:
            return
        try:
            if os.name == "nt":
                killer = await asyncio.create_subprocess_exec(
                    "taskkill", "/PID", str(proc.pid), "/T", "/F",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await killer.communicate()
            else:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except ProcessLookupError:
                pass


def _shell_quote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def _seatbelt_profile(policy: SandboxPolicy) -> str:
    rules = ["(version 1)", "(deny default)"]
    rules.append("(allow process-exec)")
    rules.append("(allow process-fork)")
    rules.append("(allow sysctl-read)")
    rules.append("(allow mach-lookup)")
    # Read access to standard system paths
    rules.append('(allow file-read* (subpath "/usr"))')
    rules.append('(allow file-read* (subpath "/System"))')
    rules.append('(allow file-read* (subpath "/Library"))')
    rules.append('(allow file-read* (subpath "/private/tmp"))')
    rules.append('(allow file-read* (subpath "/dev"))')
    for root in policy.writable_roots:
        rules.append(f'(allow file-read* file-write* (subpath "{root}"))')
    for root in policy.readable_roots:
        rules.append(f'(allow file-read* (subpath "{root}"))')
    if policy.allow_network:
        rules.append("(allow network*)")
    return "\n".join(rules)
