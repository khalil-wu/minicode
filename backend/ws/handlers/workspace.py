from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from backend.ws.handler import WebSocketSession

logger = logging.getLogger(__name__)


async def handle_workspace_import(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.workspace_service import parse_workspace_import_request, workspace_conversation_switched_payload

    request = parse_workspace_import_request(data)
    if request.error_event is not None:
        from backend.ws.command_results import emit_command_error
        await emit_command_error(session, "workspace.import", request.error_event)
        return True

    project_path = request.project_path
    activated = await session._activate_workspace_path(
        str(project_path),
        announce=True,
        wait_for_initialize=True,
        error_command="workspace.import",
    )
    if activated and session.active_conversation_id:
        branch = session._git_branch_for(project_path)
        updated = session.conversation_repo.update_workspace_binding(
            session.active_conversation_id,
            workspace_root=str(project_path),
            git_branch=branch,
            worktree_path="",
            git_isolated=False,
        )
        if updated is not None:
            await session._send_ws_payload(
                workspace_conversation_switched_payload(updated),
                log_context="conversation.switched",
            )
        await session._send_conversation_list()
    return True


async def handle_workspace_recent(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.workspace_service import list_workspace_recent_payload

    await session._send_ws_payload(list_workspace_recent_payload(), log_context="workspace.recent.list")
    return True


async def handle_workspace_set(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.ws.command_results import emit_command_error

    path_str = str(data.get("path", "")).strip()
    if not path_str:
        await emit_command_error(session, "workspace.set", "Path is required")
        return True
    return await handle_workspace_import(session, {"path": path_str})


async def handle_git_pr_status(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.workspace_service import fetch_git_pr_status_payload

    await session._send_ws_payload(
        await fetch_git_pr_status_payload(session._current_workspace_root()),
        log_context="git.pr_status",
    )
    return True


HANDLERS: dict[str, Any] = {
    "workspace.import": handle_workspace_import,
    "workspace.switch": handle_workspace_import,
    "workspace.recent": handle_workspace_recent,
    "workspace.set": handle_workspace_set,
    "git.pr_status": handle_git_pr_status,
}
