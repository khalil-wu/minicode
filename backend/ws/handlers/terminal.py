from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from typing import Any, TYPE_CHECKING

from backend.agent.message import AgentEvent
from backend.runtime_env import sanitized_subprocess_env
from backend.terminal.shell_commands import normalize_windows_shell_command
from backend.tools.base import PermissionLevel

if TYPE_CHECKING:
    from backend.ws.handler import WebSocketSession

logger = logging.getLogger(__name__)


async def handle_terminal_create(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    cwd = str(data.get("cwd", "")).strip() or None
    if not cwd:
        workspace_context = getattr(session, "_workspace_context", None)
        workspace_root = getattr(workspace_context, "root_path", None)
        if workspace_root is not None:
            cwd = str(workspace_root)
    try:
        cwd_path = session._resolve_workspace_cwd(cwd)
        terminal_session = await session.terminal_manager.create_session(
            cwd=str(cwd_path),
            on_output=session._on_terminal_output,
            on_exit=session._on_terminal_exit,
        )
        session.active_terminal_session_id = terminal_session.session_id
        await session._send_ws_payload({
            "type": "terminal.created",
            "session_id": terminal_session.session_id,
            "pid": terminal_session.pid,
            "shell": terminal_session.shell,
            "cwd": terminal_session._initial_cwd,
        }, log_context="terminal.created")
    except Exception as exc:
        logger.error("Terminal creation failed: %s", exc, exc_info=True)
        await session._send_event(AgentEvent.error(f"Terminal creation failed: {exc}", recoverable=True))
    return True


async def handle_terminal_input(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    session_id = str(data.get("session_id", ""))
    input_data = str(data.get("data", ""))
    terminal_session = session.terminal_manager.get_session(session_id)
    if terminal_session is None:
        await session._send_event(
            AgentEvent.error(f"Terminal session '{session_id}' not found", recoverable=True)
        )
        return True
    session.active_terminal_session_id = session_id
    try:
        await terminal_session.send_input(input_data)
    except RuntimeError as exc:
        await session._send_event(AgentEvent.error(str(exc), recoverable=True))
    return True


async def handle_terminal_resize(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    """Resize a terminal session's PTY window."""
    session_id = str(data.get("session_id") or data.get("sessionId") or "").strip()
    if not session_id:
        active_id = getattr(session, "active_terminal_session_id", "")
        session_id = str(active_id or "").strip()
    if not session_id:
        await session._send_event(
            AgentEvent.error("No terminal session to resize", recoverable=True)
        )
        return True

    cols = int(data.get("cols") or data.get("columns") or 80)
    rows = int(data.get("rows") or 24)

    terminal_session = session.terminal_manager.get_session(session_id)
    if terminal_session is None:
        await session._send_event(
            AgentEvent.error(f"Terminal session '{session_id}' not found", recoverable=True)
        )
        return True

    # Try to resize the underlying process window
    resized = False
    proc = getattr(terminal_session, '_process', None)
    if proc is not None and proc.returncode is None:
        try:
            import signal, fcntl, termios, struct, os
            # Unix: send SIGWINCH after setting window size
            if hasattr(proc, '_transport') and hasattr(proc._transport, 'get_extra_info'):
                pty_fd = proc._transport.get_extra_info('pty_fd')
                if pty_fd is not None:
                    winsize = struct.pack('HHHH', rows, cols, 0, 0)
                    fcntl.ioctl(pty_fd, termios.TIOCSWINSZ, winsize)
                    os.kill(proc.pid, signal.SIGWINCH)
                    resized = True
            elif not getattr(terminal_session, '_is_windows', False):
                # Non-PTY Unix process: at least send SIGWINCH
                try:
                    os.kill(proc.pid, signal.SIGWINCH)
                    resized = True
                except (OSError, ProcessLookupError):
                    pass
        except ImportError:
            # Windows or missing modules — best-effort
            pass

    await session._send_ws_payload({
        "type": "terminal.resized",
        "session_id": session_id,
        "cols": cols,
        "rows": rows,
        "applied": resized,
    }, log_context="terminal.resize")
    return True


async def handle_terminal_kill(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    session_id = str(data.get("session_id", ""))
    destroyed = await session.terminal_manager.destroy_session(session_id)
    if destroyed:
        if getattr(session, "active_terminal_session_id", None) == session_id:
            session.active_terminal_session_id = None
        await session._send_ws_payload({
            "type": "terminal.killed",
            "session_id": session_id,
        }, log_context="terminal.killed")
    else:
        await session._send_event(
            AgentEvent.error(f"Terminal session '{session_id}' not found", recoverable=True)
        )
    return True


async def handle_terminal_list(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    sessions = session.terminal_manager.list_sessions()
    await session._send_ws_payload({
        "type": "terminal.list",
        "sessions": [
            {
                "session_id": s.session_id,
                "pid": s.pid,
                "cwd": s.cwd,
                "shell": s.shell,
                "is_alive": s.is_alive,
                "started_at": s.started_at,
            }
            for s in sessions
        ],
    }, log_context="terminal.list")
    return True


async def handle_terminal_snapshot_request(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    session_id = str(data.get("session_id") or data.get("sessionId") or "").strip()
    if not session_id:
        active_id = getattr(session, "active_terminal_session_id", "")
        session_id = str(active_id or "").strip()
    if not session_id:
        sessions = session.terminal_manager.list_sessions()
        session_id = sessions[-1].session_id if sessions else ""
    if not session_id:
        await session._send_ws_payload({
            "type": "terminal.snapshot",
            "session_id": "",
            "output": "",
            "truncated": False,
            "error": "No terminal session is available",
        }, log_context="terminal.snapshot")
        return True

    try:
        max_chars = int(data.get("max_chars") or data.get("maxChars") or 20_000)
    except (TypeError, ValueError):
        max_chars = 20_000
    snapshot = session.terminal_manager.snapshot(session_id, max_chars=max_chars)
    if snapshot is None:
        await session._send_ws_payload({
            "type": "terminal.snapshot",
            "session_id": session_id,
            "output": "",
            "truncated": False,
            "error": f"Terminal session '{session_id}' not found",
        }, log_context="terminal.snapshot")
        return True
    session.active_terminal_session_id = session_id
    payload = {"type": "terminal.snapshot", **snapshot}
    await session._send_ws_payload(payload, log_context="terminal.snapshot")
    return True


def _mirror_session_id(data: dict[str, Any]) -> str:
    return str(data.get("session_id") or data.get("sessionId") or "").strip()[:128]


def _optional_int(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


async def handle_terminal_mirror_created(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    session_id = _mirror_session_id(data)
    if not session_id:
        await session._send_event(AgentEvent.error("terminal.mirror.created requires session_id", recoverable=True))
        return True
    try:
        session.terminal_manager.upsert_external_session(
            session_id,
            cwd=str(data.get("cwd") or "").strip() or None,
            shell=str(data.get("shell") or "").strip() or None,
            pid=_optional_int(data.get("pid")),
            is_alive=True,
        )
    except RuntimeError as exc:
        await session._send_event(AgentEvent.error(str(exc), recoverable=True))
        return True
    session.active_terminal_session_id = session_id
    return True


async def handle_terminal_mirror_output(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    session_id = _mirror_session_id(data)
    if not session_id:
        await session._send_event(AgentEvent.error("terminal.mirror.output requires session_id", recoverable=True))
        return True
    chunk = str(data.get("data") or data.get("output") or "")
    if len(chunk) > 20_000:
        chunk = chunk[-20_000:]
    try:
        session.terminal_manager.append_external_output(
            session_id,
            chunk,
            cwd=str(data.get("cwd") or "").strip() or None,
            shell=str(data.get("shell") or "").strip() or None,
            pid=_optional_int(data.get("pid")),
        )
    except RuntimeError as exc:
        await session._send_event(AgentEvent.error(str(exc), recoverable=True))
        return True
    session.active_terminal_session_id = session_id
    return True


async def handle_terminal_mirror_exit(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    session_id = _mirror_session_id(data)
    if not session_id:
        await session._send_event(AgentEvent.error("terminal.mirror.exit requires session_id", recoverable=True))
        return True
    session.terminal_manager.mark_external_exit(session_id)
    if getattr(session, "active_terminal_session_id", None) == session_id:
        session.active_terminal_session_id = session_id
    return True


async def handle_terminal_exec(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    command = str(data.get("command", "")).strip()
    if not command:
        await session._send_event(AgentEvent.error("Command is required", recoverable=True))
        return True
    if len(command) > 4096:
        await session._send_event(AgentEvent.error("Command exceeds maximum length (4096 characters)", recoverable=True))
        return True

    # Normalize Windows shell commands (e.g., curl -> curl.exe to avoid PowerShell alias)
    command = normalize_windows_shell_command(command)

    cwd = str(session._resolve_workspace_cwd(str(data.get("cwd", "")).strip() or None))
    checker = session.permission_checker.with_workspace_root(Path(cwd))
    command_args = {"command": command, "cwd": cwd}
    perm_level = checker.check("terminal.exec", command_args, context=session.permission_context)
    denial = checker.get_denial_reason("terminal.exec", command_args, context=session.permission_context)
    if denial or perm_level == PermissionLevel.ALWAYS_DENY:
        await session._send_ws_payload({
            "type": "terminal.output",
            "command": command,
            "output": denial or "terminal.exec is blocked by the current permission policy.",
            "exit_code": -1,
        }, log_context="terminal.output")
        return True
    if perm_level in {PermissionLevel.CONFIRM, PermissionLevel.DIFF_REVIEW}:
        request_id = f"terminal_exec_{uuid.uuid4().hex}"
        event = AgentEvent.approval_request(
            tool_call_id=request_id,
            tool_name="terminal.exec",
            args=command_args,
        )
        conversation_id = str(getattr(session, "active_conversation_id", "") or "").strip()
        if conversation_id:
            event.data["conversation_id"] = conversation_id
        payload = session._build_approval_request_payload(event)
        await session._send_ws_payload(payload, log_context="terminal.approval_request")
        approval = await session._approval_handler(request_id)
        if not isinstance(approval, dict) or approval.get("action") != "approve":
            guidance = str((approval or {}).get("guidance") or "terminal command rejected").strip()
            await session._send_ws_payload({
                "type": "terminal.output",
                "command": command,
                "output": f"Command rejected: {guidance}",
                "exit_code": -1,
            }, log_context="terminal.output")
            return True
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=sanitized_subprocess_env({"MINICODE_TERMINAL_EXEC": "1"}),
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        output = stdout.decode("utf-8", errors="replace")
        if stderr:
            output += "\n" + stderr.decode("utf-8", errors="replace")
        await session._send_ws_payload({
            "type": "terminal.output",
            "command": command,
            "output": output[:10000],
            "exit_code": proc.returncode,
        }, log_context="terminal.output")
    except asyncio.TimeoutError:
        await session._send_ws_payload({
            "type": "terminal.output",
            "command": command,
            "output": "Command timed out (30s limit)",
            "exit_code": -1,
        }, log_context="terminal.output")
    except Exception as exc:
        await session._send_ws_payload({
            "type": "terminal.output",
            "command": command,
            "output": f"Error: {exc}",
            "exit_code": -1,
        }, log_context="terminal.output")
    return True


HANDLERS: dict[str, Any] = {
    "terminal.create": handle_terminal_create,
    "terminal.input": handle_terminal_input,
    "terminal.resize": handle_terminal_resize,
    "terminal.kill": handle_terminal_kill,
    "terminal.list": handle_terminal_list,
    "terminal.snapshot.request": handle_terminal_snapshot_request,
    "terminal.mirror.created": handle_terminal_mirror_created,
    "terminal.mirror.output": handle_terminal_mirror_output,
    "terminal.mirror.exit": handle_terminal_mirror_exit,
    "terminal.exec": handle_terminal_exec,
}
