"""Sandbox runner — executes commands under OS-level isolation."""
from __future__ import annotations

import asyncio
import codecs
import locale
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


def _preferred_oem_encoding() -> str:
    """Best-effort name of the encoding native console tools emit on this host.

    On Windows this is the OEM/ANSI codepage (e.g. cp936 on zh-CN), which is what
    git/cmd/dir write when they have not been told to use UTF-8. Elsewhere it is
    the locale preferred encoding. Used as a fallback when bytes are not valid
    UTF-8 so non-ASCII output is not replaced with U+FFFD garbage.
    """
    if sys.platform == "win32":
        try:
            import ctypes  # noqa: PLC0415

            cp = ctypes.windll.kernel32.GetOEMCP()
            if cp:
                return f"cp{cp}"
        except Exception:
            pass
    try:
        return locale.getpreferredencoding(False) or "utf-8"
    except Exception:
        return "utf-8"


def _decode_command_bytes(data: bytes) -> str:
    """Decode finished command output, tolerating non-UTF-8 native tool output.

    Tries strict UTF-8 first (the common case once children are nudged toward
    UTF-8), then the host OEM/locale encoding for legacy native tools, and only
    then falls back to lossy UTF-8 so we never raise.
    """
    if not data:
        return ""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        fallback = _preferred_oem_encoding()
        if fallback.lower() not in ("utf-8", "utf8"):
            try:
                return data.decode(fallback)
            except (UnicodeDecodeError, LookupError):
                pass
        return data.decode("utf-8", errors="replace")



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
        stream_callback: Callable[..., Awaitable[None]] | None = None,
    ) -> SandboxResult:
        env = self._build_env()
        wrapped_command = self._wrap_command(command)
        process_kwargs = self._process_kwargs()

        proc: asyncio.subprocess.Process | None = None
        stdout_buf = bytearray()
        stderr_buf = bytearray()

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

            async def _forward_stream(piece: str, stream_name: str) -> None:
                if not stream_callback or not piece:
                    return
                try:
                    await stream_callback(piece, stream_name)
                except TypeError:
                    try:
                        await stream_callback(piece)
                    except Exception:
                        pass
                except Exception:
                    pass

            async def _read_stream(
                stream: Any, sink: bytearray, *, stream_name: str
            ) -> int:
                # Accumulate raw bytes and cap on byte length; the final decode
                # handles UTF-8/native fallback. For live forwarding we use an
                # incremental UTF-8 decoder so multi-byte characters split across
                # read() boundaries are not corrupted mid-stream.
                total = 0
                decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
                while stream is not None:
                    chunk = await stream.read(4096)
                    if not chunk:
                        break
                    remaining = MAX_OUTPUT_LENGTH - total
                    if remaining > 0:
                        sink += chunk[:remaining]
                    total += len(chunk)
                    if stream_callback:
                        piece = decoder.decode(chunk)
                        await _forward_stream(piece, stream_name)
                if stream_callback:
                    tail = decoder.decode(b"", final=True)
                    await _forward_stream(tail, stream_name)
                return total

            try:
                stdout_task = asyncio.create_task(
                    _read_stream(proc.stdout, stdout_buf, stream_name="stdout")
                )
                stderr_task = asyncio.create_task(
                    _read_stream(proc.stderr, stderr_buf, stream_name="stderr")
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
                    stdout=_decode_command_bytes(bytes(stdout_buf)),
                    stderr=_decode_command_bytes(bytes(stderr_buf)),
                    exit_code=-1,
                    timed_out=True,
                )

            if cancel_task:
                cancel_task.cancel()

            if cancel_event and cancel_event.is_set():
                return SandboxResult(
                    stdout=_decode_command_bytes(bytes(stdout_buf)),
                    stderr=_decode_command_bytes(bytes(stderr_buf)),
                    exit_code=-1,
                    cancelled=True,
                )

            return SandboxResult(
                stdout=_decode_command_bytes(bytes(stdout_buf)),
                stderr=_decode_command_bytes(bytes(stderr_buf)),
                exit_code=proc.returncode or 0,
            )

        except FileNotFoundError:
            return SandboxResult(stdout="", stderr=f"Command not found: {command}", exit_code=127)
        except OSError as exc:
            return SandboxResult(stdout="", stderr=str(exc), exit_code=1)

    def _build_env(self) -> dict[str, str]:
        env = sanitized_subprocess_env()
        # Nudge child processes toward UTF-8 so their output decodes cleanly.
        # PYTHONUTF8/PYTHONIOENCODING cover Python children; PYTHONUNBUFFERED
        # keeps streamed output prompt. Native Windows tools that ignore these
        # are handled by the OEM-codepage fallback in _decode_command_bytes.
        env.setdefault("PYTHONUTF8", "1")
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env.setdefault("PYTHONUNBUFFERED", "1")
        env.update(self._policy.env_overrides)
        return env

    def _wrap_command(self, command: str) -> str:
        if self._policy.disable_os_sandbox:
            return command

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
