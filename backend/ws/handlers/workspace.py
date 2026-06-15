from __future__ import annotations

import asyncio
import logging
import shutil
from typing import Any, TYPE_CHECKING

from backend.agent.message import AgentEvent

if TYPE_CHECKING:
    from backend.ws.handler import WebSocketSession

logger = logging.getLogger(__name__)


async def handle_workspace_import(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.workspace.path_utils import build_missing_path_hint, normalize_project_import_path

    path_str = str(data.get("path", "")).strip()
    if not path_str:
        await session._send_event(AgentEvent.error("Project path is required", recoverable=True))
        return True

    project_path = normalize_project_import_path(path_str)
    if not project_path.exists() or not project_path.is_dir():
        hint = build_missing_path_hint(path_str)
        message = f"Invalid project path: {path_str}"
        if hint:
            message = f"{message}. {hint}"
        await session._send_event(
            AgentEvent.error(
                message,
                recoverable=True,
                error_type="workspace",
                error_code="workspace_missing",
            )
        )
        return True

    activated = await session._activate_workspace_path(
        str(project_path),
        announce=True,
        wait_for_initialize=True,
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
                {
                    "type": "conversation.switched",
                    "conversation_id": updated.id,
                    "conversation": updated.to_dict(),
                    "is_hydrating": False,
                },
                log_context="conversation.switched",
            )
        await session._send_conversation_list()
    return True


async def handle_workspace_recent(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.workspace.recent_projects import RecentProjectStore

    store = RecentProjectStore()
    projects = store.list()
    await session._send_ws_payload({
        "type": "workspace.recent.list",
        "projects": [project.to_dict() for project in projects],
    }, log_context="workspace.recent.list")
    return True


async def handle_workspace_set(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    path_str = str(data.get("path", "")).strip()
    if not path_str:
        await session._send_event(AgentEvent.error("Path is required", recoverable=True))
        return True
    return await handle_workspace_import(session, {"path": path_str})


async def handle_git_pr_status(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    gh_path = shutil.which("gh")
    if not gh_path:
        await session._send_ws_payload(
            {"type": "git.pr_status", "error": "gh CLI not found", "pr": None, "checks": []},
            log_context="git.pr_status",
        )
        return True

    cwd = str(session._current_workspace_root())

    async def run_gh(*args: str) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            gh_path, *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
        return proc.returncode or 0, (stdout or stderr or b"").decode(errors="replace").strip()

    pr_info: dict[str, Any] | None = None
    checks: list[dict[str, str]] = []

    try:
        code, out = await run_gh("pr", "view", "--json", "number,title,state,url,headRefName,statusCheckRollup")
        if code == 0 and out:
            import json as _json
            raw = _json.loads(out)
            pr_info = {
                "number": raw.get("number"),
                "title": raw.get("title", ""),
                "state": raw.get("state", ""),
                "url": raw.get("url", ""),
                "branch": raw.get("headRefName", ""),
            }
            for check in raw.get("statusCheckRollup", []) or []:
                checks.append({
                    "name": check.get("name") or check.get("context", ""),
                    "status": (check.get("conclusion") or check.get("status") or "pending").lower(),
                    "url": check.get("detailsUrl") or check.get("targetUrl", ""),
                })
    except Exception as exc:
        await session._send_ws_payload(
            {"type": "git.pr_status", "error": str(exc), "pr": None, "checks": []},
            log_context="git.pr_status",
        )
        return True

    await session._send_ws_payload(
        {"type": "git.pr_status", "pr": pr_info, "checks": checks},
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
