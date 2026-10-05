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

from backend.runtime_env import sanitized_subprocess_env, vault_subprocess_env
from backend.subprocesses import spawn_exec, terminate_process_tree
from backend.terminal.shell_commands import windows_powershell_native_tool_alias_prelude
from backend.tools.output_limits import (
    TERMINAL_OUTPUT_DEFAULT_CHARS,
    TERMINAL_OUTPUT_MAX_CHARS,
)

logger = logging.getLogger(__name__)


MAX_SESSIONS = 5


def _require_conversation_owner(conversation_id: str) -> str:
    owner = str(conversation_id or "").strip()
    if not owner:
        raise RuntimeError("Terminal conversation owner is required")
    return owner


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
    terminal_mode: str = "pipe"
    exit_code: int | None = None
    exit_signal: int | str | None = None
    exited_at: float | None = None
    cleanup_pending: bool = False
    cleanup_reason: str = ""


class TerminalSession:
    def __init__(
        self,
        session_id: str,
        cwd: str | None = None,
        on_output: Callable[[str, str, int, int], Coroutine[Any, Any, None]] | None = None,
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
        self._output_cursor = 0
        self._MAX_OUTPUT_BUFFER_CHARS = TERMINAL_OUTPUT_MAX_CHARS
        self._stdout_reader_task: asyncio.Task[None] | None = None
        self._stderr_reader_task: asyncio.Task[None] | None = None
        self._waiter_task: asyncio.Task[None] | None = None
        self._is_windows = platform.system() == "Windows"
        self._exit_notified = False
        self._exit_notification_error: dict[str, str] = {}
        self._external_pid: int | None = None
        self._external_alive: bool | None = None
        self._external_exit_code: int | None = None
        self._external_exit_signal: int | str | None = None
        self._exited_at: float | None = None
        # Set when a kill could not prove the shell tree exited. The session
        # then stays registered as the recovery handle for that process.
        self.cleanup_pending = False
        self.cleanup_reason = ""

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
    def terminal_mode(self) -> str:
        return "pty" if self._process is None and self._external_pid is not None else "pipe"

    @property
    def exit_code(self) -> int | None:
        return self._process.returncode if self._process is not None else self._external_exit_code

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
            terminal_mode=self.terminal_mode,
            exit_code=self.exit_code,
            exit_signal=self._external_exit_signal,
            exited_at=self._exited_at,
            cleanup_pending=self.cleanup_pending,
            cleanup_reason=self.cleanup_reason,
        )

    def snapshot(self, *, max_chars: int = TERMINAL_OUTPUT_DEFAULT_CHARS) -> dict[str, Any]:
        limit = max(0, min(int(max_chars or 0), self._MAX_OUTPUT_BUFFER_CHARS))
        output = "".join(self._output_buffer)
        truncated = limit > 0 and len(output) > limit
        bounded_output = output[-limit:] if limit > 0 else ""
        output_chars = len(bounded_output.encode("utf-16-le", "surrogatepass")) // 2
        return {
            "session_id": self.session_id,
            "conversation_id": self.conversation_id,
            "pid": self.pid,
            "cwd": self._initial_cwd,
            "shell": self.shell,
            "started_at": self._started_at,
            "is_alive": self.is_alive,
            "terminal_mode": self.terminal_mode,
            "exit_code": self.exit_code,
            "exit_signal": self._external_exit_signal,
            "exited_at": self._exited_at,
            "output": bounded_output,
            "output_chars": output_chars,
            "total_output_chars": self._output_cursor,
            "output_start_cursor": self._output_cursor - output_chars,
            "output_end_cursor": self._output_cursor,
            "truncated": truncated,
            "exit_notification_error": dict(self._exit_notification_error),
            "cleanup_pending": bool(self.cleanup_pending),
            "cleanup_reason": self.cleanup_reason,
        }

    def update_external_metadata(
        self,
        *,
        cwd: str | None = None,
        shell: str | None = None,
        pid: int | None = None,
        is_alive: bool = True,
        exit_code: int | None = None,
        exit_signal: int | str | None = None,
        exited_at: float | None = None,
    ) -> None:
        """Mirror metadata for a desktop PTY owned by the Electron process."""
        if cwd:
            self._initial_cwd = cwd
        if shell:
            self._shell_cmd = [shell]
        if pid is not None:
            self._external_pid = pid
        self._external_alive = is_alive
        if not is_alive and (exit_code is not None or exit_signal is not None or exited_at is not None):
            self._external_exit_code = exit_code
            self._external_exit_signal = exit_signal
            self._exited_at = exited_at
        if not self._started_at:
            self._started_at = time.time()

    def set_external_snapshot(self, output: str, end_cursor: int) -> None:
        if end_cursor < self._output_cursor:
            return
        self._output_buffer = [output] if output else []
        self._output_cursor = end_cursor
        self._trim_output_buffer()

    def append_external_output(self, data: str, *, start_cursor: int | None = None, end_cursor: int | None = None) -> None:
        if not data:
            return
        if start_cursor is not None and end_cursor is not None:
            if end_cursor <= self._output_cursor:
                return
            if start_cursor < self._output_cursor:
                data = data.encode("utf-16-le", "surrogatepass")[(self._output_cursor - start_cursor) * 2:].decode("utf-16-le", "surrogatepass")
            elif start_cursor > self._output_cursor:
                self._output_buffer.clear()
                self._output_cursor = start_cursor
        self._append_output(str(data))

    def clear_output(self) -> None:
        """Forget reconnectable scrollback without stopping the shell."""
        self._output_buffer.clear()

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

        self._process = await spawn_exec(
            *self._shell_cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._initial_cwd,
            env=sanitized_subprocess_env({**vault_subprocess_env("run_command"), "TERM": "dumb", "NO_COLOR": "1"}),
        )
        self._started_at = time.time()
        self._output_buffer.clear()
        self._output_cursor = 0
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
            # The is_alive check above cannot close the race: the shell can shut
            # its stdin (or die) before drain() completes while returncode is
            # still None. Swallowing that dropped the user's keystroke with no
            # client-visible signal, so surface it through the same RuntimeError
            # contract the caller already reports.
            logger.warning("Terminal %s stdin is unavailable: %s", self.session_id, exc)
            raise RuntimeError(
                f"Terminal session {self.session_id} stdin is unavailable: {exc}"
            ) from exc

    async def kill(self) -> bool:
        """Terminate the shell and report whether its exit is proven.

        ``False`` means the shell may still be running: ``cleanup_pending``
        stays set and the readers keep mirroring output, so the caller must
        retain this session as the recovery handle instead of dropping it.
        """
        if self._process is None:
            # Electron/node-pty is the sole owner of desktop terminals.  The
            # backend stores only a reconnectable mirror and must never kill
            # that process when a WebSocket session expires.
            self._external_alive = False
            return True

        reaped = False
        reason = "terminal_tree_survived_kill"
        try:
            reaped = await terminate_process_tree(self._process)
            reason = "" if reaped else "terminal_tree_survived_kill"
        except ProcessLookupError:
            reaped = True
            reason = ""
        except Exception as exc:
            reason = f"terminal_kill_failed: {exc}"
            logger.warning("Error killing terminal %s: %s", self.session_id, exc)

        self.cleanup_pending = not reaped
        self.cleanup_reason = reason
        if not reaped:
            # The readers are this session's only view of a shell that is still
            # alive; cancelling them would blind the recovery handle.
            logger.warning(
                "Terminal session %s could not be proven stopped (%s)",
                self.session_id,
                reason,
            )
            return False

        await self._cancel_reader_tasks()
        logger.info("Terminal session %s killed", self.session_id)
        return True

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
                logger.warning(
                    "Terminal %s reader task failed while being cancelled",
                    self.session_id,
                    exc_info=True,
                )

        self._stdout_reader_task = None
        self._stderr_reader_task = None
        self._waiter_task = None

    async def _notify_exit_once(self) -> None:
        if self._exit_notified:
            return
        self._exit_notified = True
        if self._process is not None and self._process.returncode is not None:
            self._exited_at = time.time() * 1000

        if self._on_exit and self._process and self._process.returncode is not None:
            try:
                await self._on_exit(self.session_id, self._process.returncode)
            except Exception:
                # This callback is the only emitter of terminal.exit; swallowing
                # it leaves the panel showing a shell that already died.
                self._exit_notification_error = {
                    "kind": "terminal_exit_projection_failed",
                    "message": "terminal.exit callback failed",
                }
                logger.error(
                    "Terminal %s exit callback failed; the client was never told the "
                    "shell exited",
                    self.session_id,
                    exc_info=True,
                )

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
        excess = sum(len(s) for s in self._output_buffer) - self._MAX_OUTPUT_BUFFER_CHARS
        while excess > 0:
            first = self._output_buffer[0]
            if len(first) <= excess:
                excess -= len(self._output_buffer.pop(0))
            else:
                self._output_buffer[0] = first[excess:]
                break

    def _append_output(self, data: str) -> tuple[int, int]:
        start_cursor = self._output_cursor
        # Match Electron/JavaScript string offsets, including non-BMP output.
        self._output_cursor += len(data.encode("utf-16-le", "surrogatepass")) // 2
        self._output_buffer.append(data)
        self._trim_output_buffer()
        return start_cursor, self._output_cursor

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
                start_cursor, end_cursor = self._append_output(decoded)
                if self._on_output:
                    try:
                        await self._on_output(self.session_id, decoded, start_cursor, end_cursor)
                    except Exception:
                        logger.debug("Terminal %s stdout callback failed", self.session_id, exc_info=True)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            # This task is the only mirror of the shell's stdout. If it dies the
            # terminal appears frozen for the rest of the session, so the reason
            # must not sit at debug level.
            logger.error(
                "Terminal %s stdout reader stopped; output is no longer mirrored: %s",
                self.session_id,
                exc,
                exc_info=True,
            )

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
                start_cursor, end_cursor = self._append_output(decoded)
                if self._on_output:
                    try:
                        await self._on_output(self.session_id, decoded, start_cursor, end_cursor)
                    except Exception:
                        logger.debug("Terminal %s stderr callback failed", self.session_id, exc_info=True)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.error(
                "Terminal %s stderr reader stopped; output is no longer mirrored: %s",
                self.session_id,
                exc,
                exc_info=True,
            )


class TerminalSessionManager:
    def __init__(self, max_sessions: int = MAX_SESSIONS) -> None:
        self._sessions: dict[str, TerminalSession] = {}
        self._max_sessions = max_sessions

    async def create_session(
        self,
        cwd: str | None = None,
        on_output: Callable[[str, str, int, int], Coroutine[Any, Any, None]] | None = None,
        on_exit: Callable[[str, int], Coroutine[Any, Any, None]] | None = None,
        conversation_id: str = "",
    ) -> TerminalSession:
        owner = _require_conversation_owner(conversation_id)
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
            # Store the validated/stripped owner: every ownership check compares
            # against a stripped id, so storing the raw value made a padded
            # conversation_id produce a session nothing could address or kill.
            conversation_id=owner,
        )
        await session.start()
        self._sessions[session_id] = session
        return session

    def get_session(self, session_id: str) -> TerminalSession | None:
        return self._sessions.get(session_id)

    async def destroy_session(self, session_id: str, *, conversation_id: str = "") -> bool:
        session = self._sessions.get(session_id)
        owner = str(conversation_id or "").strip()
        if session is None or not owner or session.conversation_id != owner:
            return False
        if not await session.kill():
            # The shell may still be running, so the registry entry is the only
            # handle left for reaping it. Report the unfinished teardown instead
            # of dropping the handle and calling the terminal stopped.
            raise RuntimeError(
                f"Terminal session '{session_id}' could not be proven stopped "
                f"({session.cleanup_reason or 'terminal_cleanup_pending'})"
            )
        if self._sessions.get(session_id) is session:
            self._sessions.pop(session_id, None)
        return True

    async def destroy_sessions_for_conversation(self, conversation_id: str) -> int:
        """Kill all terminal sessions belonging to a conversation. Returns count killed."""
        owner = str(conversation_id or "").strip()
        if not owner:
            return 0
        to_kill = [
            sid for sid, session in self._sessions.items()
            if session.conversation_id == owner
        ]
        unproven: list[str] = []
        for sid in to_kill:
            session = self._sessions.get(sid)
            if session is None:
                continue
            if not await session.kill():
                unproven.append(sid)
                continue
            if self._sessions.get(sid) is session:
                self._sessions.pop(sid, None)
        if unproven:
            # Callers gate workspace deletion on this cleanup; a surviving shell
            # must not be reported as a completed teardown.
            raise RuntimeError(
                "Terminal sessions could not be proven stopped: "
                + ", ".join(sorted(unproven))
            )
        return len(to_kill)

    async def destroy_all(self) -> None:
        unproven: list[str] = []
        for session in list(self._sessions.values()):
            if not await session.kill():
                unproven.append(session.session_id)
                continue
            # Deregister by identity, one at a time. Rebuilding the whole dict
            # after the awaits above dropped any session registered during them
            # without ever killing it.
            if self._sessions.get(session.session_id) is session:
                self._sessions.pop(session.session_id, None)
        if unproven:
            raise RuntimeError(
                "Terminal sessions could not be proven stopped: "
                + ", ".join(sorted(unproven))
            )

    def list_sessions(self, conversation_id: str = "") -> list[TerminalSessionInfo]:
        """List only terminal sessions owned by the requested conversation."""
        owner = str(conversation_id or "").strip()
        if not owner:
            return []
        return [
            session.info for session in self._sessions.values()
            if session.conversation_id == owner
        ]

    def list_sessions_for_conversation(self, conversation_id: str) -> list[TerminalSessionInfo]:
        """List terminal sessions belonging to a specific conversation."""
        return self.list_sessions(conversation_id=conversation_id)

    def snapshot(
        self,
        session_id: str,
        *,
        max_chars: int = TERMINAL_OUTPUT_DEFAULT_CHARS,
        conversation_id: str = "",
    ) -> dict[str, Any] | None:
        session = self.get_session(session_id)
        owner = str(conversation_id or "").strip()
        if session is None or not owner or session.conversation_id != owner:
            return None
        return session.snapshot(max_chars=max_chars)

    def clear_output(self, session_id: str, *, conversation_id: str = "") -> bool:
        session = self.get_session(session_id)
        owner = str(conversation_id or "").strip()
        if session is None or not owner or session.conversation_id != owner:
            return False
        session.clear_output()
        return True

    def upsert_external_session(
        self,
        session_id: str,
        *,
        cwd: str | None = None,
        shell: str | None = None,
        pid: int | None = None,
        is_alive: bool = True,
        exit_code: int | None = None,
        exit_signal: int | str | None = None,
        exited_at: float | None = None,
        conversation_id: str = "",
    ) -> TerminalSession:
        owner = _require_conversation_owner(conversation_id)
        session = self._sessions.get(session_id)
        if session is None:
            dead_ids = [sid for sid, existing in self._sessions.items() if not existing.is_alive]
            for sid in dead_ids:
                self._sessions.pop(sid, None)
            if len(self._sessions) >= self._max_sessions:
                raise RuntimeError(f"Too many terminal sessions (max {self._max_sessions})")
            session = TerminalSession(session_id, cwd=cwd, conversation_id=owner)
            self._sessions[session_id] = session
        elif session.conversation_id != owner:
            raise RuntimeError("Terminal session is owned by a different conversation")
        session.update_external_metadata(
            cwd=cwd,
            shell=shell,
            pid=pid,
            is_alive=is_alive,
            exit_code=exit_code,
            exit_signal=exit_signal,
            exited_at=exited_at,
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
        conversation_id: str = "",
        start_cursor: int | None = None,
        end_cursor: int | None = None,
    ) -> TerminalSession:
        owner = _require_conversation_owner(conversation_id)
        existing = self._sessions.get(session_id)
        session = self.upsert_external_session(
            session_id,
            cwd=cwd,
            shell=shell,
            pid=pid,
            is_alive=existing.is_alive if existing is not None else True,
            conversation_id=owner,
        )
        session.append_external_output(data, start_cursor=start_cursor, end_cursor=end_cursor)
        return session

    def mark_external_exit(self, session_id: str, *, conversation_id: str = "",
                           exit_code: int | None = None, exit_signal: int | str | None = None,
                           exited_at: float | None = None) -> bool:
        session = self._sessions.get(session_id)
        owner = str(conversation_id or "").strip()
        if session is None or not owner or session.conversation_id != owner:
            return False
        session.update_external_metadata(is_alive=False, exit_code=exit_code,
                                         exit_signal=exit_signal, exited_at=exited_at)
        return True

    @property
    def count(self) -> int:
        return len(self._sessions)
