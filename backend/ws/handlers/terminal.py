from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, TYPE_CHECKING

from backend.agent.message import AgentEvent
from backend.ws.command_results import emit_command_error
from backend.ws.command_scope import CommandScope, resolve_command_scope

if TYPE_CHECKING:
    from backend.ws.handler import WebSocketSession

logger = logging.getLogger(__name__)


def _active_conversation_id(session: "WebSocketSession") -> str:
    return str(getattr(session, "active_conversation_id", "") or "").strip()


def _owned_terminal(session: "WebSocketSession", session_id: str) -> Any | None:
    return _terminal_owned_by_conversation(session, session_id, _active_conversation_id(session))


def _terminal_owned_by_conversation(
    session: "WebSocketSession",
    session_id: str,
    conversation_id: str,
) -> Any | None:
    terminal_session = session.terminal_manager.get_session(session_id)
    if terminal_session is None:
        return None
    owner = str(conversation_id or "").strip()
    if not owner or str(getattr(terminal_session, "conversation_id", "") or "") != owner:
        return None
    return terminal_session


def _requested_conversation_id(data: dict[str, Any]) -> str:
    return str(data.get("conversation_id") or "").strip()


def _known_conversation(session: "WebSocketSession", conversation_id: str) -> bool:
    return bool(conversation_id and session.conversation_repo.get_conversation(conversation_id) is not None)


async def _resolve_terminal_scope(
    session: "WebSocketSession",
    data: dict[str, Any],
    command: str,
) -> CommandScope | None:
    try:
        return resolve_command_scope(session, data)
    except ValueError as exc:
        await emit_command_error(session, command, exc)
        return None


async def handle_terminal_create(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.terminal_service import terminal_created_payload

    scope = await _resolve_terminal_scope(session, data, "terminal.create")
    if scope is None:
        return True
    conversation_id = scope.conversation_id
    cwd = str(data.get("cwd", "")).strip() or None
    if not cwd:
        cwd = scope.workspace_root
    try:
        cwd_path = session._resolve_workspace_cwd(cwd)
        terminal_session = await session.terminal_manager.create_session(
            cwd=str(cwd_path),
            on_output=lambda sid, chunk: session._on_terminal_output(sid, chunk, conversation_id),
            on_exit=lambda sid, code: session._on_terminal_exit(sid, code, conversation_id),
            conversation_id=conversation_id,
        )
        session.active_terminal_session_id = terminal_session.session_id
        payload = terminal_created_payload(terminal_session)
        scope.apply(payload)
        await session._send_ws_payload(payload, log_context="terminal.created")
        await session._emit_command_result(
            "terminal.create",
            "",
            data={
                "session_id": terminal_session.session_id,
                "conversation_id": scope.conversation_id,
                "workspace_root": scope.workspace_root,
            },
        )
    except Exception as exc:
        logger.error("Terminal creation failed: %s", exc, exc_info=True)
        await emit_command_error(session, "terminal.create", f"Terminal creation failed: {exc}")
    return True


async def handle_terminal_input(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    session_id = str(data.get("session_id", ""))
    input_data = str(data.get("data", ""))
    terminal_session = _owned_terminal(session, session_id)
    if terminal_session is None:
        await emit_command_error(session, "terminal.input", f"Terminal session '{session_id}' not found")
        return True
    session.active_terminal_session_id = session_id
    try:
        await terminal_session.send_input(input_data)
    except RuntimeError as exc:
        await emit_command_error(session, "terminal.input", exc)
    return True


async def handle_terminal_resize(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    """Resize a terminal session's PTY window."""
    from backend.services.terminal_service import apply_terminal_resize, resolve_terminal_session_id, terminal_resized_payload

    session_id = resolve_terminal_session_id(data, active_session_id=str(getattr(session, "active_terminal_session_id", "") or ""))
    if not session_id:
        await emit_command_error(session, "terminal.resize", "No terminal session to resize")
        return True

    cols = int(data.get("cols") or data.get("columns") or 80)
    rows = int(data.get("rows") or 24)

    terminal_session = _owned_terminal(session, session_id)
    if terminal_session is None:
        await emit_command_error(session, "terminal.resize", f"Terminal session '{session_id}' not found")
        return True

    resized = apply_terminal_resize(terminal_session, cols=cols, rows=rows)

    await session._send_ws_payload(
        terminal_resized_payload(
            session_id=session_id,
            cols=cols,
            rows=rows,
            applied=resized,
            conversation_id=_active_conversation_id(session),
        ),
        log_context="terminal.resize",
    )
    return True


async def handle_terminal_kill(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.terminal_service import terminal_killed_payload

    session_id = str(data.get("session_id", ""))
    if _owned_terminal(session, session_id) is None:
        await emit_command_error(session, "terminal.kill", f"Terminal session '{session_id}' not found")
        return True
    try:
        destroyed = await session.terminal_manager.destroy_session(
            session_id,
            conversation_id=_active_conversation_id(session),
        )
    except RuntimeError as exc:
        await emit_command_error(session, "terminal.kill", exc)
        return True
    if destroyed:
        if getattr(session, "active_terminal_session_id", None) == session_id:
            session.active_terminal_session_id = None
        conversation_id = _active_conversation_id(session)
        await session._send_ws_payload(
            terminal_killed_payload(session_id, conversation_id=conversation_id),
            log_context="terminal.killed",
        )
        await session._emit_command_result(
            "terminal.kill",
            "Terminal stopped",
            data={"session_id": session_id, "conversation_id": conversation_id},
        )
    else:
        await emit_command_error(session, "terminal.kill", f"Terminal session '{session_id}' not found")
    return True


async def handle_terminal_restart(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    """Replace one owned web terminal only after its deletion is confirmed."""
    from backend.services.terminal_service import terminal_created_payload, terminal_killed_payload

    session_id = str(data.get("session_id", "")).strip()
    conversation_id = _active_conversation_id(session)
    terminal_session = _terminal_owned_by_conversation(session, session_id, conversation_id)
    if terminal_session is None:
        await emit_command_error(session, "terminal.restart", f"Terminal session '{session_id}' not found")
        return True

    cwd = str(getattr(terminal_session, "cwd", "") or "").strip() or None
    try:
        destroyed = await session.terminal_manager.destroy_session(
            session_id,
            conversation_id=conversation_id,
        )
        if not destroyed:
            await emit_command_error(session, "terminal.restart", f"Terminal session '{session_id}' was not removed")
            return True

        if getattr(session, "active_terminal_session_id", None) == session_id:
            session.active_terminal_session_id = None
        await session._send_ws_payload(
            terminal_killed_payload(session_id, conversation_id=conversation_id),
            log_context="terminal.killed",
        )

        replacement = await session.terminal_manager.create_session(
            cwd=cwd,
            on_output=lambda sid, chunk: session._on_terminal_output(sid, chunk, conversation_id),
            on_exit=lambda sid, code: session._on_terminal_exit(sid, code, conversation_id),
            conversation_id=conversation_id,
        )
        session.active_terminal_session_id = replacement.session_id
        await session._send_ws_payload(
            terminal_created_payload(replacement),
            log_context="terminal.created",
        )
        await session._send_event(
            AgentEvent.command_result(
                "terminal.restart",
                "Terminal restarted",
                level="success",
                data={
                    "old_session_id": session_id,
                    "session_id": replacement.session_id,
                    "conversation_id": conversation_id,
                },
            )
        )
    except Exception as exc:
        logger.error("Terminal restart failed: %s", exc, exc_info=True)
        await emit_command_error(
            session,
            "terminal.restart",
            f"Terminal restart failed: {exc}",
            data={"old_session_id": session_id, "conversation_id": conversation_id},
        )
    return True


async def handle_terminal_list(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.terminal_service import terminal_list_payload

    scope = await _resolve_terminal_scope(session, data, "terminal.list")
    if scope is None:
        return True
    sessions = session.terminal_manager.list_sessions_for_conversation(scope.conversation_id)
    payload = terminal_list_payload(sessions, conversation_id=scope.conversation_id)
    scope.apply(payload)
    await session._send_ws_payload(
        payload,
        log_context="terminal.list",
    )
    await session._emit_command_result(
        "terminal.list",
        "",
        data={
            "conversation_id": scope.conversation_id,
            "workspace_root": scope.workspace_root,
            "session_count": len(sessions),
        },
    )
    return True


async def handle_terminal_snapshot_request(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.terminal_service import (
        normalize_snapshot_max_chars,
        resolve_terminal_session_id,
        terminal_snapshot_payload,
    )

    scope = await _resolve_terminal_scope(session, data, "terminal.snapshot.request")
    if scope is None:
        return True
    conversation_id = scope.conversation_id
    session_id = resolve_terminal_session_id(
        data,
        active_session_id=str(getattr(session, "active_terminal_session_id", "") or ""),
    )
    if session_id and _terminal_owned_by_conversation(session, session_id, conversation_id) is None:
        explicitly_requested = bool(str(data.get("session_id") or "").strip())
        if explicitly_requested:
            payload = terminal_snapshot_payload(
                None,
                session_id=session_id,
                conversation_id=conversation_id,
            )
            scope.apply(payload)
            await session._send_ws_payload(payload, log_context="terminal.snapshot")
            return True
        session_id = ""
    if not session_id:
        sessions = session.terminal_manager.list_sessions_for_conversation(conversation_id)
        session_id = sessions[-1].session_id if sessions else ""
    if not session_id:
        payload = terminal_snapshot_payload(None, conversation_id=conversation_id)
        scope.apply(payload)
        await session._send_ws_payload(payload, log_context="terminal.snapshot")
        return True

    max_chars = normalize_snapshot_max_chars(data)
    if _terminal_owned_by_conversation(session, session_id, conversation_id) is None:
        payload = terminal_snapshot_payload(
                None,
                session_id=session_id,
                conversation_id=conversation_id,
            )
        scope.apply(payload)
        await session._send_ws_payload(payload, log_context="terminal.snapshot")
        return True
    snapshot = session.terminal_manager.snapshot(
        session_id,
        max_chars=max_chars,
        conversation_id=conversation_id,
    )
    if snapshot is None:
        payload = terminal_snapshot_payload(
                None,
                session_id=session_id,
                conversation_id=conversation_id,
            )
        scope.apply(payload)
        await session._send_ws_payload(payload, log_context="terminal.snapshot")
        return True
    session.active_terminal_session_id = session_id
    payload = terminal_snapshot_payload(snapshot)
    scope.apply(payload)
    await session._send_ws_payload(payload, log_context="terminal.snapshot")
    return True


async def handle_terminal_clear(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    """Clear owned reconnectable scrollback while leaving the shell alive."""
    from backend.services.terminal_service import (
        resolve_terminal_session_id,
        terminal_snapshot_payload,
    )

    scope = await _resolve_terminal_scope(session, data, "terminal.clear")
    if scope is None:
        return True
    session_id = resolve_terminal_session_id(
        data,
        active_session_id=str(getattr(session, "active_terminal_session_id", "") or ""),
    )
    if not session_id:
        await emit_command_error(session, "terminal.clear", "No terminal session to clear")
        return True
    if not session.terminal_manager.clear_output(
        session_id,
        conversation_id=scope.conversation_id,
    ):
        await emit_command_error(session, "terminal.clear", f"Terminal session '{session_id}' not found")
        return True

    snapshot = session.terminal_manager.snapshot(
        session_id,
        conversation_id=scope.conversation_id,
    )
    payload = terminal_snapshot_payload(snapshot)
    scope.apply(payload)
    await session._send_ws_payload(payload, log_context="terminal.snapshot")
    await session._emit_command_result(
        "terminal.clear",
        "Terminal scrollback cleared",
        data={
            "session_id": session_id,
            "conversation_id": scope.conversation_id,
            "workspace_root": scope.workspace_root,
        },
    )
    return True


def _mirror_session_id(data: dict[str, Any]) -> str:
    from backend.services.terminal_service import mirror_session_id

    return mirror_session_id(data)


def _optional_int(value: Any) -> int | None:
    from backend.services.terminal_service import optional_int

    return optional_int(value)


async def handle_terminal_mirror_created(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    session_id = _mirror_session_id(data)
    if not session_id:
        await emit_command_error(session, "terminal.mirror.created", "terminal.mirror.created requires session_id")
        return True
    conversation_id = _requested_conversation_id(data)
    if not _known_conversation(session, conversation_id):
        await emit_command_error(
            session,
            "terminal.mirror.created",
            "terminal.mirror.created requires an existing conversation owner",
        )
        return True
    try:
        session.terminal_manager.upsert_external_session(
            session_id,
            cwd=str(data.get("cwd") or "").strip() or None,
            shell=str(data.get("shell") or "").strip() or None,
            pid=_optional_int(data.get("pid")),
            is_alive=data.get("is_alive") is not False,
            conversation_id=conversation_id,
        )
    except RuntimeError as exc:
        await emit_command_error(session, "terminal.mirror.created", exc)
        return True
    if conversation_id == _active_conversation_id(session):
        session.active_terminal_session_id = session_id
    return True


async def handle_terminal_mirror_output(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.terminal_service import mirror_output_chunk

    session_id = _mirror_session_id(data)
    if not session_id:
        await emit_command_error(session, "terminal.mirror.output", "terminal.mirror.output requires session_id")
        return True
    conversation_id = _requested_conversation_id(data)
    if _terminal_owned_by_conversation(session, session_id, conversation_id) is None:
        await emit_command_error(session, "terminal.mirror.output", f"Terminal session '{session_id}' not found")
        return True
    chunk = mirror_output_chunk(data)
    try:
        session.terminal_manager.append_external_output(
            session_id,
            chunk,
            cwd=str(data.get("cwd") or "").strip() or None,
            shell=str(data.get("shell") or "").strip() or None,
            pid=_optional_int(data.get("pid")),
            conversation_id=conversation_id,
        )
    except RuntimeError as exc:
        await emit_command_error(session, "terminal.mirror.output", exc)
        return True
    if conversation_id == _active_conversation_id(session):
        session.active_terminal_session_id = session_id
    return True


async def handle_terminal_mirror_exit(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    session_id = _mirror_session_id(data)
    if not session_id:
        await emit_command_error(session, "terminal.mirror.exit", "terminal.mirror.exit requires session_id")
        return True
    conversation_id = _requested_conversation_id(data)
    if _terminal_owned_by_conversation(session, session_id, conversation_id) is None:
        await emit_command_error(session, "terminal.mirror.exit", f"Terminal session '{session_id}' not found")
        return True
    session.terminal_manager.mark_external_exit(
        session_id,
        conversation_id=conversation_id,
    )
    # The mirror record survives the exit (mark_external_exit only flips
    # is_alive), so the implicit terminal target must be released the same way
    # terminal.kill/terminal.restart release it. Leaving it set makes
    # terminal.resize / terminal.clear / terminal.snapshot.request keep
    # addressing the exited terminal instead of falling back to the newest one.
    if getattr(session, "active_terminal_session_id", None) == session_id:
        session.active_terminal_session_id = None
    return True


async def handle_terminal_exec(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.permissions.context import ToolExecutionContext
    from backend.sandbox.policy import sandbox_policy_for_permission_context
    from backend.services.terminal_service import (
        parse_terminal_exec_command,
        run_terminal_exec_command,
        terminal_output_payload,
    )

    command_request = parse_terminal_exec_command(data)
    if command_request.error_event is not None:
        await emit_command_error(session, "terminal.exec", command_request.error_event)
        return True
    command = command_request.command

    scope = await _resolve_terminal_scope(session, data, "terminal.exec")
    if scope is None:
        return True
    conversation_id = scope.conversation_id
    from backend.services.workspace_service import resolve_workspace_cwd

    workspace_root = Path(scope.workspace_root).resolve()
    cwd = str(resolve_workspace_cwd(workspace_root, str(data.get("cwd", "")).strip() or None))
    checker = session.permission_checker.with_workspace_root(workspace_root)
    target_conversation = session.conversation_repo.get_conversation(conversation_id)
    if target_conversation is None:
        await emit_command_error(
            session,
            "terminal.exec",
            "terminal.exec requires an existing conversation owner",
        )
        return True
    from dataclasses import replace

    target_permission = replace(
        session._permission_context_for_conversation(
            target_conversation,
            source="terminal.exec",
        ),
        conversation_id=conversation_id,
        workspace_root=workspace_root,
    )
    sandbox_policy = sandbox_policy_for_permission_context(
        workspace_root,
        target_permission,
    )
    tool = session.tool_registry.get_tool("run_command")
    if tool is None:
        await session._send_ws_payload(
            terminal_output_payload(
                command,
                "run_command is unavailable in this session.",
                -1,
                conversation_id=conversation_id,
            ),
            log_context="terminal.output",
        )
        return True
    async def emit_tool_event(event: AgentEvent) -> None:
        if event.type != "approval_request":
            return
        event.data["conversation_id"] = conversation_id
        payload = session._build_approval_request_payload(event)
        await session._send_ws_payload(payload, log_context="terminal.approval_request")

    await session._send_ws_payload(
        await run_terminal_exec_command(
            command,
            cwd,
            tool=tool,
            context=ToolExecutionContext(
                permission=target_permission,
                session_id=session.session_id,
                conversation_id=conversation_id,
                workspace_root=workspace_root,
                allow_network=sandbox_policy.allow_network,
                sandbox_policy=sandbox_policy,
                permission_checker=checker,
                artifact_store=session.artifact_store,
                background_manager=session.background_manager,
                terminal_manager=session.terminal_manager,
            ),
            conversation_id=conversation_id,
            approval_handler=session._approval_handler,
            event_handler=emit_tool_event,
        ),
        log_context="terminal.output",
    )
    return True


HANDLERS: dict[str, Any] = {
    "terminal.create": handle_terminal_create,
    "terminal.input": handle_terminal_input,
    "terminal.resize": handle_terminal_resize,
    "terminal.kill": handle_terminal_kill,
    "terminal.restart": handle_terminal_restart,
    "terminal.list": handle_terminal_list,
    "terminal.snapshot.request": handle_terminal_snapshot_request,
    "terminal.clear": handle_terminal_clear,
    "terminal.mirror.created": handle_terminal_mirror_created,
    "terminal.mirror.output": handle_terminal_mirror_output,
    "terminal.mirror.exit": handle_terminal_mirror_exit,
    "terminal.exec": handle_terminal_exec,
}
