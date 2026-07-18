from __future__ import annotations

import asyncio
import codecs
import logging
import os
import platform
import signal
import subprocess
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Coroutine

from backend.runtime_env import sanitized_subprocess_env
from backend.terminal.shell_commands import windows_powershell_native_tool_alias_prelude

logger = logging.getLogger(__name__)

MAX_OUTPUT_LENGTH = 20_000
DEFAULT_TIMEOUT_MS = 120_000
MAX_TIMEOUT_MS = 600_000
MAX_SESSIONS = 5


def _windows_powershell_init_command() -> str:
    return (
        "$__minicodeUtf8 = [System.Text.UTF8Encoding]::new($false); "
        "[Console]::InputEncoding = $__minicodeUtf8; "
        "[Console]::OutputEncoding = $__minicodeUtf8; "
        "chcp 65001 | Out-Null; "
        f"{windows_powershell_native_tool_alias_prelude()}"
        "Clear-Host\n"
    )


@dataclass
class TerminalSessionInfo:
    session_id: str
    pid: int | None = None
    cwd: str = ""
    shell: str = ""
    started_at: float = 0.0
    is_alive: bool = False
    conversation_id: str = ""


class TerminalSession:
    def __init__(
        self,
        session_id: str,
        cwd: str | None = None,
        on_output: Callable[[str, str], Coroutine[Any, Any, None]] | None = None,
        on_exit: Callable[[str, int], Coroutine[Any, Any, None]] | None = None,
        conversation_id: str = "",
    ) -> None:
        self.session_id = session_id
        self.conversation_id = conversation_id
        self._initial_cwd = cwd or os.getcwd()
        self._on_output = on_output
        self._on_exit = on_exit
        self._process: asyncio.subprocess.Process | None = None
        self._shell_cmd: list[str] = []
        self._started_at = 0.0
        self._output_buffer: list[str] = []
        self._MAX_OUTPUT_BUFFER_CHARS = 80_000
        self._cmd_lock = asyncio.Lock()
        self._stdout_reader_task: asyncio.Task[None] | None = None
        self._stderr_reader_task: asyncio.Task[None] | None = None
        self._waiter_task: asyncio.Task[None] | None = None
        self._is_windows = platform.system() == "Windows"
        self._exit_notified = False
        self._external_pid: int | None = None
        self._external_alive: bool | None = None

    @property
    def is_alive(self) -> bool:
        if self._process is not None:
            return self._process.returncode is None
        return bool(self._external_alive)

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process else self._external_pid

    @property
    def shell(self) -> str:
        return " ".join(self._shell_cmd) if self._shell_cmd else ""

    @property
    def info(self) -> TerminalSessionInfo:
        return TerminalSessionInfo(
            session_id=self.session_id,
            pid=self.pid,
            cwd=self._initial_cwd,
            shell=self.shell,
            started_at=self._started_at,
            is_alive=self.is_alive,
            conversation_id=self.conversation_id,
        )

    def snapshot(self, *, max_chars: int = 20_000) -> dict[str, Any]:
        limit = max(0, min(int(max_chars or 0), self._MAX_OUTPUT_BUFFER_CHARS))
        output = "".join(self._output_buffer)
        truncated = limit > 0 and len(output) > limit
        bounded_output = output[-limit:] if limit > 0 else ""
        return {
            "session_id": self.session_id,
            "pid": self.pid,
            "cwd": self._initial_cwd,
            "shell": self.shell,
            "started_at": self._started_at,
            "is_alive": self.is_alive,
            "output": bounded_output,
            "output_chars": len(bounded_output),
            "total_output_chars": len(output),
            "truncated": truncated,
        }

    def update_external_metadata(
        self,
        *,
        cwd: str | None = None,
        shell: str | None = None,
        pid: int | None = None,
        is_alive: bool = True,
    ) -> None:
        """Mirror metadata for a desktop PTY owned by the Electron process."""
        if cwd:
            self._initial_cwd = cwd
        if shell:
            self._shell_cmd = [shell]
        if pid is not None:
            self._external_pid = pid
        self._external_alive = is_alive
        if not self._started_at:
            self._started_at = time.time()

    def append_external_output(self, data: str) -> None:
        if not data:
            return
        self._output_buffer.append(str(data))
        self._trim_output_buffer()

    async def start(self) -> None:
        if self.is_alive:
            return

        if self._is_windows:
            self._shell_cmd = [
                "powershell.exe",
                "-NoProfile",
                "-NoLogo",
                "-NoExit",
            ]
        else:
            shell = os.environ.get("SHELL", "/bin/bash")
            self._shell_cmd = [shell, "--norc", "--noprofile", "-i"]

        self._process = await asyncio.create_subprocess_exec(
            *self._shell_cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._initial_cwd,
            env=sanitized_subprocess_env({"TERM": "dumb", "NO_COLOR": "1"}),
            **({"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if self._is_windows else {}),
        )
        self._started_at = time.time()
        self._output_buffer.clear()
        self._exit_notified = False
        self._stdout_reader_task = asyncio.create_task(self._read_stdout())
        self._stderr_reader_task = asyncio.create_task(self._read_stderr())
        self._waiter_task = asyncio.create_task(self._wait_for_exit())

        if self._is_windows and self._process.stdin:
            init_cmd = _windows_powershell_init_command()
            try:
                self._process.stdin.write(init_cmd.encode("utf-8"))
                await self._process.stdin.drain()
            except Exception:
                logger.debug("Terminal %s PowerShell init command failed", self.session_id, exc_info=True)

        logger.info(
            "Terminal session %s started (PID %s, shell: %s, cwd: %s)",
            self.session_id,
            self.pid,
            self.shell,
            self._initial_cwd,
        )

    async def send_input(self, data: str) -> None:
        if not self.is_alive or not self._process or not self._process.stdin:
            raise RuntimeError(f"Terminal session {self.session_id} is not running")

        try:
            # Keep raw keystrokes intact so interactive shells behave normally.
            payload = data
            if self._is_windows:
                # xterm Enter emits '\r', while redirected PowerShell stdin expects '\n'.
                payload = payload.replace("\r", "\n")
            self._process.stdin.write(payload.encode("utf-8"))
            await self._process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            logger.warning("Terminal %s stdin is unavailable: %s", self.session_id, exc)

    async def execute_command(
        self,
        command: str,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ) -> tuple[str, int]:
        async with self._cmd_lock:
            return await self._execute_command_locked(command, timeout_ms)

    async def _execute_command_locked(
        self,
        command: str,
        timeout_ms: int,
    ) -> tuple[str, int]:
        if not self.is_alive:
            await self.start()

        if not self._process or not self._process.stdin:
            raise RuntimeError("Terminal is not available")

        marker = f"__MINICODE_CMD_END_{uuid.uuid4().hex[:8]}__"
        exit_code_marker = f"__MINICODE_EXIT_{uuid.uuid4().hex[:8]}__"

        if self._is_windows:
            full_cmd = (
                f"{command}\n"
                f'Write-Output "{exit_code_marker}$LASTEXITCODE"\n'
                f'Write-Output "{marker}"\n'
            )
        else:
            full_cmd = (
                f"{command}\n"
                f'echo "{exit_code_marker}$?"\n'
                f'echo "{marker}"\n'
            )

        start_index = len(self._output_buffer)

        try:
            self._process.stdin.write(full_cmd.encode("utf-8"))
            await self._process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            return "Terminal input channel is unavailable", -1

        timeout_sec = min(timeout_ms, MAX_TIMEOUT_MS) / 1000.0
        deadline = time.monotonic() + timeout_sec

        while time.monotonic() < deadline:
            await asyncio.sleep(0.05)

            new_output = "".join(self._output_buffer[start_index:])
            if marker not in new_output:
                if not self.is_alive and self._process and self._process.returncode is not None:
                    break
                continue

            output, exit_code = self._extract_command_result(
                new_output,
                marker=marker,
                exit_code_marker=exit_code_marker,
            )
            return output, exit_code

        return f"Command timed out after {timeout_sec:.0f}s", -1

    async def kill(self) -> None:
        if self._process is None:
            return

        try:
            if self._is_windows:
                self._process.kill()
            else:
                self._process.send_signal(signal.SIGTERM)
                try:
                    await asyncio.wait_for(self._process.wait(), timeout=3)
                except asyncio.TimeoutError:
                    self._process.kill()
        except ProcessLookupError:
            pass
        except Exception as exc:
            logger.warning("Error killing terminal %s: %s", self.session_id, exc)

        await self._cancel_reader_tasks()
        logger.info("Terminal session %s killed", self.session_id)

    def _extract_command_result(
        self,
        output: str,
        *,
        marker: str,
        exit_code_marker: str,
    ) -> tuple[str, int]:
        exit_code = -1
        output_lines: list[str] = []

        for line in output.splitlines():
            if marker in line:
                break
            if exit_code_marker in line:
                raw_code = line.replace(exit_code_marker, "", 1).strip()
                try:
                    exit_code = int(raw_code)
                except (TypeError, ValueError):
                    exit_code = -1
                continue
            output_lines.append(line)

        final_output = "\n".join(output_lines).strip()
        if len(final_output) > MAX_OUTPUT_LENGTH:
            total_length = len(final_output)
            final_output = (
                final_output[:MAX_OUTPUT_LENGTH]
                + f"\n\n[output truncated: showing first {MAX_OUTPUT_LENGTH} of {total_length} chars]"
            )
        return final_output, exit_code

    async def _cancel_reader_tasks(self) -> None:
        for task in (self._stdout_reader_task, self._stderr_reader_task, self._waiter_task):
            if task and not task.done():
                task.cancel()

        for task in (self._stdout_reader_task, self._stderr_reader_task, self._waiter_task):
            if not task:
                continue
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass

        self._stdout_reader_task = None
        self._stderr_reader_task = None
        self._waiter_task = None

    async def _notify_exit_once(self) -> None:
        if self._exit_notified:
            return
        self._exit_notified = True

        if self._on_exit and self._process and self._process.returncode is not None:
            try:
                await self._on_exit(self.session_id, self._process.returncode)
            except Exception:
                logger.debug("Terminal %s exit callback failed", self.session_id, exc_info=True)

    async def _wait_for_exit(self) -> None:
        if not self._process:
            return

        try:
            await self._process.wait()
        except asyncio.CancelledError:
            return
        finally:
            await self._notify_exit_once()

    def _trim_output_buffer(self) -> None:
        total = sum(len(s) for s in self._output_buffer)
        if total <= self._MAX_OUTPUT_BUFFER_CHARS:
            return
        while self._output_buffer and total > self._MAX_OUTPUT_BUFFER_CHARS:
            removed = len(self._output_buffer.pop(0))
            total -= removed

    async def _read_stdout(self) -> None:
        if not self._process or not self._process.stdout:
            return

        # Incremental decoder: a multibyte UTF-8 char split across two 4096-byte
        # reads must not be replaced with U+FFFD garbage.
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        try:
            while True:
                chunk = await self._process.stdout.read(4096)
                if not chunk:
                    break
                decoded = decoder.decode(chunk)
                if not decoded:
                    continue
                self._output_buffer.append(decoded)
                self._trim_output_buffer()
                if self._on_output:
                    try:
                        await self._on_output(self.session_id, decoded)
                    except Exception:
                        logger.debug("Terminal %s stdout callback failed", self.session_id, exc_info=True)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.debug("Terminal %s stdout reader error: %s", self.session_id, exc)

    async def _read_stderr(self) -> None:
        if not self._process or not self._process.stderr:
            return

        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        try:
            while True:
                chunk = await self._process.stderr.read(4096)
                if not chunk:
                    break
                decoded = decoder.decode(chunk)
                if not decoded:
                    continue
                self._output_buffer.append(decoded)
                self._trim_output_buffer()
                if self._on_output:
                    try:
                        await self._on_output(self.session_id, decoded)
                    except Exception:
                        logger.debug("Terminal %s stderr callback failed", self.session_id, exc_info=True)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.debug("Terminal %s stderr reader error: %s", self.session_id, exc)


class TerminalSessionManager:
    def __init__(self, max_sessions: int = MAX_SESSIONS) -> None:
        self._sessions: dict[str, TerminalSession] = {}
        self._max_sessions = max_sessions

    async def create_session(
        self,
        cwd: str | None = None,
        on_output: Callable[[str, str], Coroutine[Any, Any, None]] | None = None,
        on_exit: Callable[[str, int], Coroutine[Any, Any, None]] | None = None,
        conversation_id: str = "",
    ) -> TerminalSession:
        dead_ids = [sid for sid, session in self._sessions.items() if not session.is_alive]
        for sid in dead_ids:
            self._sessions.pop(sid, None)

        if len(self._sessions) >= self._max_sessions:
            raise RuntimeError(f"Too many terminal sessions (max {self._max_sessions})")

        session_id = f"term_{uuid.uuid4().hex[:8]}"
        session = TerminalSession(
            session_id=session_id,
            cwd=cwd,
            on_output=on_output,
            on_exit=on_exit,
            conversation_id=conversation_id,
        )
        await session.start()
        self._sessions[session_id] = session
        return session

    def get_session(self, session_id: str) -> TerminalSession | None:
        return self._sessions.get(session_id)

    async def destroy_session(self, session_id: str) -> bool:
        session = self._sessions.pop(session_id, None)
        if session is None:
            return False
        await session.kill()
        return True

    async def destroy_sessions_for_conversation(self, conversation_id: str) -> int:
        """Kill all terminal sessions belonging to a conversation. Returns count killed."""
        to_kill = [
            sid for sid, session in self._sessions.items()
            if session.conversation_id == conversation_id
        ]
        for sid in to_kill:
            session = self._sessions.pop(sid, None)
            if session is not None:
                await session.kill()
        return len(to_kill)

    async def destroy_all(self) -> None:
        for session in list(self._sessions.values()):
            await session.kill()
        self._sessions.clear()

    def list_sessions(self, conversation_id: str = "") -> list[TerminalSessionInfo]:
        """List terminal sessions, optionally filtered by conversation_id."""
        if conversation_id:
            return [
                session.info for session in self._sessions.values()
                if session.conversation_id == conversation_id
            ]
        return [session.info for session in self._sessions.values()]

    def list_sessions_for_conversation(self, conversation_id: str) -> list[TerminalSessionInfo]:
        """List terminal sessions belonging to a specific conversation."""
        return self.list_sessions(conversation_id=conversation_id)

    def snapshot(self, session_id: str, *, max_chars: int = 20_000) -> dict[str, Any] | None:
        session = self.get_session(session_id)
        if session is None:
            return None
        return session.snapshot(max_chars=max_chars)

    def upsert_external_session(
        self,
        session_id: str,
        *,
        cwd: str | None = None,
        shell: str | None = None,
        pid: int | None = None,
        is_alive: bool = True,
    ) -> TerminalSession:
        session = self._sessions.get(session_id)
        if session is None:
            dead_ids = [sid for sid, existing in self._sessions.items() if not existing.is_alive]
            for sid in dead_ids:
                self._sessions.pop(sid, None)
            if len(self._sessions) >= self._max_sessions:
                raise RuntimeError(f"Too many terminal sessions (max {self._max_sessions})")
            session = TerminalSession(session_id, cwd=cwd)
            self._sessions[session_id] = session
        session.update_external_metadata(
            cwd=cwd,
            shell=shell,
            pid=pid,
            is_alive=is_alive,
        )
        return session

    def append_external_output(
        self,
        session_id: str,
        data: str,
        *,
        cwd: str | None = None,
        shell: str | None = None,
        pid: int | None = None,
    ) -> TerminalSession:
        session = self.upsert_external_session(
            session_id,
            cwd=cwd,
            shell=shell,
            pid=pid,
            is_alive=True,
        )
        session.append_external_output(data)
        return session

    def mark_external_exit(self, session_id: str) -> bool:
        session = self._sessions.get(session_id)
        if session is None:
            return False
        session.update_external_metadata(is_alive=False)
        return True

    @property
    def count(self) -> int:
        return len(self._sessions)
