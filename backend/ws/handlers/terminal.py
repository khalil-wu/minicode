from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, TYPE_CHECKING

from backend.agent.message import AgentEvent
from backend.runtime_env import sanitized_subprocess_env

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
        await session._send_ws_payload({
            "type": "terminal.created",
            "session_id": terminal_session.session_id,
            "pid": terminal_session.pid,
            "shell": terminal_session.shell,
            "cwd": terminal_session._initial_cwd,
        }, log_context="terminal.created")
    except Exception as exc:
        logger.error("Terminal creation failed: %s", exc, exc_info=True)
        await session._send_event(AgentEvent.error(f"终端创建失败: {exc}", recoverable=True))
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
    try:
        await terminal_session.send_input(input_data)
    except RuntimeError as exc:
        await session._send_event(AgentEvent.error(str(exc), recoverable=True))
    return True


async def handle_terminal_resize(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    return True


async def handle_terminal_kill(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    session_id = str(data.get("session_id", ""))
    destroyed = await session.terminal_manager.destroy_session(session_id)
    if destroyed:
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


async def handle_terminal_exec(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    command = str(data.get("command", "")).strip()
    if not command:
        await session._send_event(AgentEvent.error("Command is required", recoverable=True))
        return True
    if len(command) > 4096:
        await session._send_event(AgentEvent.error("Command exceeds maximum length (4096 characters)", recoverable=True))
        return True

    cwd = str(session._resolve_workspace_cwd(str(data.get("cwd", "")).strip() or None))
    checker = session.permission_checker.with_workspace_root(Path(cwd))
    command_args = {"command": command, "cwd": cwd}
    perm_level = checker.check("terminal.exec", command_args, context=session.permission_context)
    denial = checker.get_denial_reason("terminal.exec", command_args, context=session.permission_context)
    if denial or perm_level.name in {"CONFIRM", "DIFF_REVIEW", "ALWAYS_DENY"}:
        await session._send_ws_payload({
            "type": "terminal.output",
            "command": command,
            "output": denial or "terminal.exec requires agent tool approval; use run_command for approved execution.",
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
    "terminal.exec": handle_terminal_exec,
}
