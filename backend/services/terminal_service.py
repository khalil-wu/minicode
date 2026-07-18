from __future__ import annotations

import asyncio
import os
import subprocess
from dataclasses import dataclass
from typing import Any

from backend.agent.message import AgentEvent
from backend.runtime_env import sanitized_subprocess_env
from backend.terminal.shell_commands import normalize_windows_shell_command


@dataclass(frozen=True)
class TerminalCommandRequest:
    command: str
    error_event: AgentEvent | None = None


def resolve_terminal_session_id(data: dict[str, Any], *, active_session_id: str = "") -> str:
    session_id = str(data.get("session_id") or data.get("sessionId") or "").strip()
    if session_id:
        return session_id
    return str(active_session_id or "").strip()


def terminal_created_payload(terminal_session: Any) -> dict[str, Any]:
    return {
        "type": "terminal.created",
        "session_id": terminal_session.session_id,
        "pid": terminal_session.pid,
        "shell": terminal_session.shell,
        "cwd": terminal_session._initial_cwd,
    }


def terminal_resized_payload(*, session_id: str, cols: int, rows: int, applied: bool) -> dict[str, Any]:
    return {
        "type": "terminal.resized",
        "session_id": session_id,
        "cols": cols,
        "rows": rows,
        "applied": bool(applied),
    }


def apply_terminal_resize(terminal_session: Any, *, cols: int, rows: int) -> bool:
    proc = getattr(terminal_session, "_process", None)
    if proc is None or proc.returncode is not None:
        return False
    try:
        import fcntl
        import signal
        import struct
        import termios

        if hasattr(proc, "_transport") and hasattr(proc._transport, "get_extra_info"):
            pty_fd = proc._transport.get_extra_info("pty_fd")
            if pty_fd is not None:
                winsize = struct.pack("HHHH", rows, cols, 0, 0)
                fcntl.ioctl(pty_fd, termios.TIOCSWINSZ, winsize)
                os.kill(proc.pid, signal.SIGWINCH)
                return True
        elif not getattr(terminal_session, "_is_windows", False):
            try:
                os.kill(proc.pid, signal.SIGWINCH)
                return True
            except (OSError, ProcessLookupError):
                return False
    except ImportError:
        return False
    return False


def terminal_killed_payload(session_id: str) -> dict[str, Any]:
    return {"type": "terminal.killed", "session_id": session_id}


def terminal_list_payload(sessions: list[Any]) -> dict[str, Any]:
    return {
        "type": "terminal.list",
        "sessions": [
            {
                "session_id": item.session_id,
                "pid": item.pid,
                "cwd": item.cwd,
                "shell": item.shell,
                "is_alive": item.is_alive,
                "started_at": item.started_at,
            }
            for item in sessions
        ],
    }


def normalize_snapshot_max_chars(data: dict[str, Any]) -> int:
    try:
        return int(data.get("max_chars") or data.get("maxChars") or 20_000)
    except (TypeError, ValueError):
        return 20_000


def terminal_snapshot_payload(snapshot: dict[str, Any] | None, *, session_id: str = "") -> dict[str, Any]:
    if snapshot is None:
        return {
            "type": "terminal.snapshot",
            "session_id": session_id,
            "output": "",
            "truncated": False,
            "error": (
                f"Terminal session '{session_id}' not found"
                if session_id
                else "No terminal session is available"
            ),
        }
    return {"type": "terminal.snapshot", **snapshot}


def mirror_session_id(data: dict[str, Any]) -> str:
    return str(data.get("session_id") or data.get("sessionId") or "").strip()[:128]


def optional_int(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def mirror_output_chunk(data: dict[str, Any], *, max_chars: int = 20_000) -> str:
    chunk = str(data.get("data") or data.get("output") or "")
    if len(chunk) > max_chars:
        chunk = chunk[-max_chars:]
    return chunk


def terminal_output_payload(command: str, output: str, exit_code: int | None) -> dict[str, Any]:
    return {
        "type": "terminal.output",
        "command": command,
        "output": output,
        "exit_code": exit_code,
    }


def parse_terminal_exec_command(data: dict[str, Any]) -> TerminalCommandRequest:
    command = str(data.get("command", "")).strip()
    if not command:
        return TerminalCommandRequest(
            command="",
            error_event=AgentEvent.error("Command is required", recoverable=True),
        )
    if len(command) > 4096:
        return TerminalCommandRequest(
            command="",
            error_event=AgentEvent.error("Command exceeds maximum length (4096 characters)", recoverable=True),
        )
    return TerminalCommandRequest(command=normalize_windows_shell_command(command))


def terminal_exec_approval_event(
    *,
    request_id: str,
    command_args: dict[str, Any],
    conversation_id: str = "",
) -> AgentEvent:
    event = AgentEvent.approval_request(
        tool_call_id=request_id,
        tool_name="terminal.exec",
        args=command_args,
    )
    clean_conversation_id = str(conversation_id or "").strip()
    if clean_conversation_id:
        event.data["conversation_id"] = clean_conversation_id
    return event


def terminal_exec_rejected_payload(command: str, approval: Any) -> dict[str, Any]:
    guidance = str((approval or {}).get("guidance") or "terminal command rejected").strip()
    return terminal_output_payload(command, f"Command rejected: {guidance}", -1)


def terminal_exec_completed_payload(command: str, stdout: bytes, stderr: bytes, exit_code: int | None) -> dict[str, Any]:
    output = stdout.decode("utf-8", errors="replace")
    if stderr:
        output += "\n" + stderr.decode("utf-8", errors="replace")
    return terminal_output_payload(command, output[:10000], exit_code)


async def run_terminal_exec_command(
    command: str,
    cwd: str,
    *,
    timeout: float = 30,
    create_subprocess_shell: Any | None = None,
) -> dict[str, Any]:
    shell_factory = create_subprocess_shell or asyncio.create_subprocess_shell
    try:
        proc = await shell_factory(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=sanitized_subprocess_env({"MINICODE_TERMINAL_EXEC": "1"}),
            **({"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if os.name == "nt" else {}),
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return terminal_exec_completed_payload(command, stdout, stderr, getattr(proc, "returncode", None))
    except asyncio.TimeoutError:
        timeout_label = str(int(timeout)) if float(timeout).is_integer() else str(timeout)
        return terminal_output_payload(command, f"Command timed out ({timeout_label}s limit)", -1)
    except Exception as exc:
        return terminal_output_payload(command, f"Error: {exc}", -1)
