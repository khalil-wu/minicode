from __future__ import annotations

import asyncio
import logging
from typing import Any, TYPE_CHECKING

from backend.ws.command_scope import resolve_command_scope

if TYPE_CHECKING:
    from backend.ws.handler import WebSocketSession

logger = logging.getLogger(__name__)


async def _activate_workspace_for_command(
    session: "WebSocketSession",
    data: dict[str, Any],
    *,
    command: str,
) -> bool:
    """Opening a project starts a new conversation; history keeps its owner."""
    from backend.ws.command_results import emit_command_error
    from backend.ws.handlers.conversation import handle_conversation_create

    project_path = str(data.get("path") or "").strip()
    if not project_path:
        await emit_command_error(session, command, "Project path is required")
        return True
    return await handle_conversation_create(
        session,
        {
            "workspace_root": project_path,
            "title": "New chat",
            "conversation_type": "main",
            "permission_mode": data.get("permission_mode"),
        },
        command=command,
    )


async def handle_workspace_import(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    return await _activate_workspace_for_command(session, data, command="workspace.import")


async def handle_workspace_switch(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    return await _activate_workspace_for_command(session, data, command="workspace.switch")


async def handle_workspace_recent(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.workspace_service import list_workspace_recent_payload

    payload = await asyncio.to_thread(list_workspace_recent_payload)
    await session.send_payload(payload, log_context="workspace.recent.list")
    return True


async def handle_workspace_recent_remove(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.workspace_service import remove_workspace_recent
    from backend.workspace.recent_projects import RecentProjectPersistenceError
    from backend.ws.command_results import emit_command_error

    path = str(data.get("path") or "").strip()
    if not path:
        await emit_command_error(session, "workspace.recent.remove", "Path is required")
        return True
    try:
        removed, payload = await asyncio.to_thread(remove_workspace_recent, path)
    except RecentProjectPersistenceError:
        logger.exception("Failed to persist removal of recent workspace metadata")
        await session.emit_command_result(
            "workspace.recent.remove",
            "Recent workspace metadata could not be saved; the list was left unchanged and no project files were touched.",
            level="error",
            data={"path": path, "reason": "persistence_failed", "retryable": True},
        )
        return True
    await session.send_payload(payload, log_context="workspace.recent.list")
    await session.emit_command_result(
        "workspace.recent.remove",
        "Recent workspace entry removed." if removed else "Recent workspace entry was already absent.",
        level="success",
        data={"path": path, "removed": removed},
    )
    return True


async def handle_workspace_recent_clear(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.workspace_service import clear_workspace_recent
    from backend.workspace.recent_projects import RecentProjectPersistenceError

    try:
        removed, payload = await asyncio.to_thread(clear_workspace_recent)
    except RecentProjectPersistenceError:
        logger.exception("Failed to persist clearing recent workspace metadata")
        await session.emit_command_result(
            "workspace.recent.clear",
            "Recent workspace metadata could not be saved; the list was left unchanged and no project files were touched.",
            level="error",
            data={"reason": "persistence_failed", "retryable": True},
        )
        return True
    await session.send_payload(payload, log_context="workspace.recent.list")
    await session.emit_command_result(
        "workspace.recent.clear",
        "Recent workspace list cleared.",
        level="success",
        data={"removed_count": removed},
    )
    return True


async def handle_workspace_set(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    """Desktop "open folder" activation.

    The renderer sends the path as ``path``; extensions and restored state use
    the ``workspace_root``/``workspaceRoot`` spellings, so all three are accepted
    here and normalized before the shared activation path runs.
    """

    path_str = str(
        data.get("path")
        or data.get("workspace_root")
        or data.get("workspaceRoot")
        or ""
    ).strip()
    return await _activate_workspace_for_command(
        session,
        {**data, "path": path_str},
        command="workspace.set",
    )


async def handle_git_pr_status(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.workspace_service import fetch_git_pr_status_payload
    from backend.ws.command_results import emit_command_error

    try:
        scope = resolve_command_scope(session, data)
        payload = await fetch_git_pr_status_payload(scope.workspace_root)
    except ValueError as exc:
        await emit_command_error(session, "git.pr_status", exc)
        return True
    scope.apply(payload)
    await session.send_payload(
        payload,
        log_context="git.pr_status",
    )
    await _start_pr_auto_fix_if_needed(
        session,
        payload,
        conversation_id=scope.conversation_id,
    )
    return True


async def _start_pr_auto_fix_if_needed(
    session: "WebSocketSession",
    payload: dict[str, Any],
    *,
    conversation_id: str = "",
) -> None:
    automation = payload.get("automation") if isinstance(payload.get("automation"), dict) else {}
    if not automation.get("auto_fix"):
        return
    pr = payload.get("pr") if isinstance(payload.get("pr"), dict) else {}
    checks = payload.get("checks") if isinstance(payload.get("checks"), list) else []
    failed = [check for check in checks if isinstance(check, dict) and str(check.get("status") or "").lower() in {"failure", "failed", "error", "cancelled", "canceled"}]
    if not pr or not failed:
        session.last_pr_auto_fix_signature = ""
        return
    if session.run_manager.has_active_run():
        return
    signature = f"{pr.get('number')}:{','.join(sorted(str(check.get('name') or '') for check in failed))}"
    if session.last_pr_auto_fix_signature == signature:
        return
    conversation_id = str(conversation_id or "").strip()
    if not conversation_id:
        return
    failed_names = ", ".join(str(check.get("name") or "CI check") for check in failed)
    prompt = (
        f"PR #{pr.get('number')} has failing checks: {failed_names}. "
        "Inspect the failures, implement the smallest correct fix, run the relevant verification, and summarize the result."
    )
    await session.start_agent_run(
        prompt,
        conversation_id=conversation_id,
        metadata={"source": "pr_auto_fix", "pr_number": pr.get("number")},
    )
    session.last_pr_auto_fix_signature = signature


async def handle_git_pr_automation_set(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.workspace_service import set_git_pr_automation_payload
    from backend.ws.command_results import emit_command_error

    try:
        scope = resolve_command_scope(session, data)
        payload = await set_git_pr_automation_payload(scope.workspace_root, data)
    except ValueError as exc:
        await emit_command_error(session, "git.pr_automation.set", exc)
        return True
    scope.apply(payload)
    await session.send_payload(payload, log_context="git.pr_status")
    return True


HANDLERS: dict[str, Any] = {
    "workspace.import": handle_workspace_import,
    "workspace.switch": handle_workspace_switch,
    "workspace.recent": handle_workspace_recent,
    "workspace.recent.remove": handle_workspace_recent_remove,
    "workspace.recent.clear": handle_workspace_recent_clear,
    "workspace.set": handle_workspace_set,
    "git.pr_status": handle_git_pr_status,
    "git.pr_automation.set": handle_git_pr_automation_set,
}
