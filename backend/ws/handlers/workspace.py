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
    """Single owner for every explicit workspace activation command.

    ``workspace.import``, ``workspace.switch`` and ``workspace.set`` are the
    same capability, so they share one implementation: activate the path, rebind
    the active conversation, publish the authoritative switched payload and
    refresh every renderer's inventory. Errors are reported under the command the
    client actually sent so its pending state resolves.
    """

    from backend.services.workspace_service import parse_workspace_import_request, workspace_conversation_switched_payload

    request = parse_workspace_import_request(data)
    if request.error_event is not None:
        from backend.ws.command_results import emit_command_error
        await emit_command_error(session, command, request.error_event)
        return True

    project_path = request.project_path
    activated = await session.activate_workspace_path(
        str(project_path),
        announce=True,
        wait_for_initialize=True,
        error_command=command,
    )
    if activated and session.active_conversation_id:
        branch = await asyncio.to_thread(session.git_branch_for, project_path)
        updated = await asyncio.to_thread(
            session.conversation_repo.update_workspace_binding,
            session.active_conversation_id,
            workspace_root=str(project_path),
            git_branch=branch,
            worktree_path="",
            git_isolated=False,
        )
        if updated is None:
            # The conversation disappeared between activation and rebinding, so
            # the workspace the client now sees is not bound to anything. Saying
            # nothing left the UI showing a successful switch over a lost bind.
            from backend.ws.command_results import emit_command_error

            await emit_command_error(
                session,
                command,
                "The workspace was activated but its conversation no longer exists; "
                "the binding was not saved.",
                data={
                    "workspace_root": str(project_path),
                    "conversation_id": str(session.active_conversation_id or ""),
                    "reason": "conversation_missing",
                },
            )
            return True
        await session.send_payload(
            workspace_conversation_switched_payload(updated),
            log_context="conversation.switched",
        )
        from backend.ws.handlers.conversation import _broadcast_conversation_lists

        broadcast_errors = await _broadcast_conversation_lists(session)
        if broadcast_errors:
            await session.emit_command_result(
                command,
                "Workspace activated, but one or more windows need to resynchronize.",
                level="warning",
                data={
                    "workspace_root": str(project_path),
                    "projection_errors": broadcast_errors,
                },
            )
    return True


async def handle_workspace_import(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    return await _activate_workspace_for_command(session, data, command="workspace.import")


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
        data.get("path") or data.get("workspace_root") or ""
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
    "workspace.switch": handle_workspace_import,
    "workspace.recent": handle_workspace_recent,
    "workspace.recent.remove": handle_workspace_recent_remove,
    "workspace.recent.clear": handle_workspace_recent_clear,
    "workspace.set": handle_workspace_set,
    "git.pr_status": handle_git_pr_status,
    "git.pr_automation.set": handle_git_pr_automation_set,
}
