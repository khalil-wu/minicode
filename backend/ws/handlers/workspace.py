from __future__ import annotations

import asyncio
import logging
from typing import Any, TYPE_CHECKING

from backend.agent.message import AgentEvent

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
        branch = await asyncio.to_thread(session._git_branch_for, project_path)
        updated = await asyncio.to_thread(
            session.conversation_repo.update_workspace_binding,
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

    payload = await asyncio.to_thread(list_workspace_recent_payload)
    await session._send_ws_payload(payload, log_context="workspace.recent.list")
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

    payload = await fetch_git_pr_status_payload(session._current_workspace_root())
    await session._send_ws_payload(
        payload,
        log_context="git.pr_status",
    )
    await _start_pr_auto_fix_if_needed(session, payload)
    return True


async def _start_pr_auto_fix_if_needed(session: "WebSocketSession", payload: dict[str, Any]) -> None:
    automation = payload.get("automation") if isinstance(payload.get("automation"), dict) else {}
    if not automation.get("auto_fix"):
        return
    pr = payload.get("pr") if isinstance(payload.get("pr"), dict) else {}
    checks = payload.get("checks") if isinstance(payload.get("checks"), list) else []
    failed = [check for check in checks if isinstance(check, dict) and str(check.get("status") or "").lower() in {"failure", "failed", "error", "cancelled", "canceled"}]
    if not pr or not failed:
        session._last_pr_auto_fix_signature = ""
        return
    if getattr(session, "_run_manager", None).has_active_run():
        return
    signature = f"{pr.get('number')}:{','.join(sorted(str(check.get('name') or '') for check in failed))}"
    if getattr(session, "_last_pr_auto_fix_signature", "") == signature:
        return
    conversation_id = str(getattr(session, "active_conversation_id", "") or "")
    if not conversation_id:
        ensure = getattr(session, "_ensure_active_conversation", None)
        if callable(ensure):
            ensure()
        conversation_id = str(getattr(session, "active_conversation_id", "") or "")
    if not conversation_id:
        return
    failed_names = ", ".join(str(check.get("name") or "CI check") for check in failed)
    prompt = (
        f"PR #{pr.get('number')} has failing checks: {failed_names}. "
        "Inspect the failures, implement the smallest correct fix, run the relevant verification, and summarize the result."
    )
    cancel_event = asyncio.Event()
    managed = session.task_manager.create(
        "pr.auto_fix",
        session._run_agent(
            prompt,
            conversation_id=conversation_id,
            metadata={"source": "pr_auto_fix", "pr_number": pr.get("number")},
            cancel_event=cancel_event,
        ),
    )
    if managed.task is None:
        return
    session._register_agent_run(
        conversation_id=conversation_id,
        task=managed.task,
        task_id=managed.id,
        cancel_event=cancel_event,
    )
    session._last_pr_auto_fix_signature = signature

    async def _cleanup() -> None:
        try:
            await managed.task
        except Exception:
            await session._send_event(AgentEvent.error("PR 自动修复任务失败。", recoverable=True, error_type="api"))
        finally:
            session._cleanup_agent_run(
                conversation_id=conversation_id,
                task=managed.task,
                task_id=managed.id,
                cancel_event=cancel_event,
            )

    cleanup_task = asyncio.create_task(_cleanup())
    session._track_command_task(cleanup_task)


async def handle_git_pr_automation_set(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.workspace_service import set_git_pr_automation_payload
    from backend.ws.command_results import emit_command_error

    try:
        payload = await set_git_pr_automation_payload(session._current_workspace_root(), data)
    except ValueError as exc:
        await emit_command_error(session, "git.pr_automation.set", exc)
        return True
    await session._send_ws_payload(payload, log_context="git.pr_status")
    return True


HANDLERS: dict[str, Any] = {
    "workspace.import": handle_workspace_import,
    "workspace.switch": handle_workspace_import,
    "workspace.recent": handle_workspace_recent,
    "workspace.set": handle_workspace_set,
    "git.pr_status": handle_git_pr_status,
    "git.pr_automation.set": handle_git_pr_automation_set,
}
