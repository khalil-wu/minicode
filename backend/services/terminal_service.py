from __future__ import annotations

from dataclasses import dataclass
import inspect
import os
import re
from typing import Any
from uuid import uuid4

from backend.agent.context import ContextBuilder
from backend.agent.message import AgentEvent
from backend.agent.state import AgentState
from backend.agent.tool_batch_execution import execute_tool_batch
from backend.config import AgentSettings, TokenBudget
from backend.llm.base import ToolCallEvent
from backend.tools.registry import ToolRegistry
from backend.terminal.shell_commands import normalize_windows_shell_command
from backend.tools.output_limits import (
    TERMINAL_OUTPUT_DEFAULT_CHARS,
    TERMINAL_OUTPUT_MAX_CHARS,
)


@dataclass(frozen=True)
class TerminalCommandRequest:
    command: str
    error_event: AgentEvent | None = None


def resolve_terminal_session_id(data: dict[str, Any], *, active_session_id: str = "") -> str:
    session_id = str(data.get("session_id") or "").strip()
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
            or TERMINAL_OUTPUT_DEFAULT_CHARS
        )
        return max(1, min(value, TERMINAL_OUTPUT_MAX_CHARS))
    except (TypeError, ValueError):
        return TERMINAL_OUTPUT_DEFAULT_CHARS


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
    return str(data.get("session_id") or "").strip()[:128]


def optional_int(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def mirror_output_chunk(
    data: dict[str, Any], *, max_chars: int = TERMINAL_OUTPUT_MAX_CHARS
) -> str:
    chunk = str(data.get("data") or data.get("output") or "")
    limit = max(1, min(int(max_chars), TERMINAL_OUTPUT_MAX_CHARS))
    if len(chunk) > limit:
        # Keep the head and announce the truncation (cc TaskOutput always
        # surfaces a notice; silently keeping only the tail hides the start
        # of errors which is usually where the cause lives).
        omitted = len(chunk) - limit
        chunk = chunk[:limit] + f"\n[... {omitted} earlier characters truncated ...]"
    return chunk


def terminal_output_payload(
    command: str,
    output: str,
    exit_code: int | None,
    *,
    conversation_id: str = "",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "terminal.output",
        "command": command,
        "output": output,
    }
    # An unknown exit status is represented by omission on the wire.  JSON
    # null is not a number and used to disagree with TerminalOutputEvent's
    # optional numeric contract, while the UI also cannot distinguish null
    # from a successful zero exit code if it blindly coalesces the value.
    if exit_code is not None:
        payload["exit_code"] = int(exit_code)
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


async def run_terminal_exec_command(
    command: str,
    cwd: str,
    *,
    tool: Any | None = None,
    context: Any | None = None,
    conversation_id: str = "",
    approval_handler: Any | None = None,
    event_handler: Any | None = None,
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
        command_args = {"command": command, "cwd": cwd}
        tool_call_id = f"terminal_exec_{uuid4().hex}"
        registry = ToolRegistry()
        registry.register(tool)
        model_context = ContextBuilder(
            token_budget=TokenBudget(),
            agent_settings=AgentSettings(),
        )
        state = AgentState(user_message=f"terminal.exec: {command}", max_iterations=1)
        result_event: AgentEvent | None = None
        async for event in execute_tool_batch(
            [ToolCallEvent(id=tool_call_id, name="run_command", arguments=command_args)],
            ctx=model_context,
            state=state,
            tool_registry=registry,
            permission_checker=context.permission_checker,
            approval_handler=approval_handler,
            skill_manager=None,
            permission_context=context.permission,
            tool_ctx=context,
        ):
            if callable(event_handler):
                handled = event_handler(event)
                if inspect.isawaitable(handled):
                    await handled
            if event.type == "tool_result":
                result_event = event
        output = str(result_event.data.get("summary") or "") if result_event is not None else ""
        match = re.match(r"Exit code:\s*(-?\d+)", output, re.IGNORECASE)
        exit_code = int(match.group(1)) if match else (-1 if result_event is None or result_event.data.get("is_error") else 0)
        return terminal_output_payload(
            command,
            output[:30000],
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
