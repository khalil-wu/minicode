from __future__ import annotations

from dataclasses import dataclass
import os
import re
from typing import Any

from backend.agent.message import AgentEvent
from backend.terminal.shell_commands import normalize_windows_shell_command
from backend.tools.output_limits import (
    CLAUDE_BASH_OUTPUT_DEFAULT_CHARS,
    CLAUDE_BASH_OUTPUT_MAX_CHARS,
)


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
        "terminal_mode": terminal_session.terminal_mode,
        "conversation_id": str(getattr(terminal_session, "conversation_id", "") or ""),
    }


def terminal_resized_payload(
    *,
    session_id: str,
    cols: int,
    rows: int,
    applied: bool,
    conversation_id: str = "",
) -> dict[str, Any]:
    payload = {
        "type": "terminal.resized",
        "session_id": session_id,
        "cols": cols,
        "rows": rows,
        "applied": bool(applied),
    }
    if clean_conversation_id := str(conversation_id or "").strip():
        payload["conversation_id"] = clean_conversation_id
    return payload


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


def terminal_killed_payload(session_id: str, *, conversation_id: str = "") -> dict[str, Any]:
    payload = {"type": "terminal.killed", "session_id": session_id}
    if clean_conversation_id := str(conversation_id or "").strip():
        payload["conversation_id"] = clean_conversation_id
    return payload


def terminal_list_payload(sessions: list[Any], *, conversation_id: str = "") -> dict[str, Any]:
    payload = {
        "type": "terminal.list",
        "sessions": [
            {
                "session_id": item.session_id,
                "pid": item.pid,
                "cwd": item.cwd,
                "shell": item.shell,
                "is_alive": item.is_alive,
                "started_at": item.started_at,
                "terminal_mode": item.terminal_mode,
                "conversation_id": str(getattr(item, "conversation_id", "") or ""),
            }
            for item in sessions
        ],
    }
    if clean_conversation_id := str(conversation_id or "").strip():
        payload["conversation_id"] = clean_conversation_id
    return payload


def normalize_snapshot_max_chars(data: dict[str, Any]) -> int:
    try:
        value = int(
            data.get("max_chars")
            or data.get("maxChars")
            or CLAUDE_BASH_OUTPUT_DEFAULT_CHARS
        )
        return max(1, min(value, CLAUDE_BASH_OUTPUT_MAX_CHARS))
    except (TypeError, ValueError):
        return CLAUDE_BASH_OUTPUT_DEFAULT_CHARS


def terminal_snapshot_payload(
    snapshot: dict[str, Any] | None,
    *,
    session_id: str = "",
    conversation_id: str = "",
) -> dict[str, Any]:
    if snapshot is None:
        payload = {
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
        if clean_conversation_id := str(conversation_id or "").strip():
            payload["conversation_id"] = clean_conversation_id
        return payload
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


def mirror_output_chunk(
    data: dict[str, Any], *, max_chars: int = CLAUDE_BASH_OUTPUT_MAX_CHARS
) -> str:
    chunk = str(data.get("data") or data.get("output") or "")
    limit = max(1, min(int(max_chars), CLAUDE_BASH_OUTPUT_MAX_CHARS))
    if len(chunk) > limit:
        chunk = chunk[-limit:]
    return chunk


def terminal_output_payload(
    command: str,
    output: str,
    exit_code: int | None,
    *,
    conversation_id: str = "",
) -> dict[str, Any]:
    payload = {
        "type": "terminal.output",
        "command": command,
        "output": output,
        "exit_code": exit_code,
    }
    if clean_conversation_id := str(conversation_id or "").strip():
        payload["conversation_id"] = clean_conversation_id
    return payload


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
        tool_name="run_command",
        args=command_args,
    )
    clean_conversation_id = str(conversation_id or "").strip()
    if clean_conversation_id:
        event.data["conversation_id"] = clean_conversation_id
    return event


def terminal_exec_rejected_payload(
    command: str,
    approval: Any,
    *,
    conversation_id: str = "",
) -> dict[str, Any]:
    guidance = str((approval or {}).get("guidance") or "terminal command rejected").strip()
    return terminal_output_payload(
        command,
        f"Command rejected: {guidance}",
        -1,
        conversation_id=conversation_id,
    )


async def run_terminal_exec_command(
    command: str,
    cwd: str,
    *,
    tool: Any | None = None,
    context: Any | None = None,
    conversation_id: str = "",
) -> dict[str, Any]:
    """Execute terminal.exec through the registered run_command tool.

    The desktop command bridge must share the same catastrophic-command check,
    workspace sandbox, timeout, and process-tree owner as model tool calls. A
    direct spawn_shell fallback would recreate a weaker second shell boundary.
    """
    if tool is None or context is None:
        return terminal_output_payload(
            command,
            "Error: terminal.exec requires the shared run_command runtime.",
            -1,
            conversation_id=conversation_id,
        )
    try:
        result = await tool.execute({"command": command, "cwd": cwd}, context)
        output = str(getattr(result, "content", "") or "")
        preview = str(getattr(result, "artifact_preview", "") or "")
        if preview:
            output = f"{output}\n\n{preview}" if output else preview
        match = re.match(r"Exit code:\s*(-?\d+)", output, re.IGNORECASE)
        exit_code = int(match.group(1)) if match else (-1 if getattr(result, "is_error", False) else 0)
        return terminal_output_payload(
            command,
            output[:10000],
            exit_code,
            conversation_id=conversation_id,
        )
    except Exception as exc:
        return terminal_output_payload(
            command,
            f"Error: {exc}",
            -1,
            conversation_id=conversation_id,
        )
