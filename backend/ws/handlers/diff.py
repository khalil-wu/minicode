from __future__ import annotations

from typing import Any, TYPE_CHECKING

from backend.agent.message import AgentEvent

if TYPE_CHECKING:
    from backend.ws.handler import WebSocketSession


def _diff_file_payload(file: Any) -> dict[str, Any]:
    return {
        "path": file.path,
        "patch": file.patch,
        "additions": file.additions,
        "deletions": file.deletions,
        "is_binary": file.is_binary,
    }


async def handle_diff_git_working_tree(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.diff.git_integration import get_working_tree_diff, get_untracked_files

    try:
        workspace = session._resolve_requested_workspace(str(data.get("workspace") or "").strip() or None)
    except ValueError as exc:
        await session._send_event(AgentEvent.error(str(exc), recoverable=True))
        return True
    result = await get_working_tree_diff(str(workspace))
    untracked = await get_untracked_files(str(workspace))
    await session._send_event(AgentEvent(type="diff.git_working_tree", data={
        "files": [_diff_file_payload(file) for file in result.files],
        "untracked": untracked,
        "total_additions": result.total_additions,
        "total_deletions": result.total_deletions,
    }))
    return True


async def handle_diff_git_staged(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.diff.git_integration import get_staged_diff

    try:
        workspace = session._resolve_requested_workspace(str(data.get("workspace") or "").strip() or None)
    except ValueError as exc:
        await session._send_event(AgentEvent.error(str(exc), recoverable=True))
        return True
    result = await get_staged_diff(str(workspace))
    await session._send_event(AgentEvent(type="diff.git_staged", data={
        "files": [_diff_file_payload(file) for file in result.files],
        "total_additions": result.total_additions,
        "total_deletions": result.total_deletions,
    }))
    return True


async def handle_diff_git_stage_file(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.diff.git_integration import stage_file

    try:
        workspace = session._resolve_requested_workspace(str(data.get("workspace") or "").strip() or None)
        path = session._validate_git_relative_path(str(data.get("path", "")))
    except ValueError as exc:
        await session._send_event(AgentEvent.error(str(exc), recoverable=True))
        return True
    ok = await stage_file(str(workspace), path)
    await session._send_event(AgentEvent(type="diff.git_stage_file", data={"path": path, "ok": ok}))
    return True


async def handle_diff_git_unstage_file(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.diff.git_integration import unstage_file

    try:
        workspace = session._resolve_requested_workspace(str(data.get("workspace") or "").strip() or None)
        path = session._validate_git_relative_path(str(data.get("path", "")))
    except ValueError as exc:
        await session._send_event(AgentEvent.error(str(exc), recoverable=True))
        return True
    ok = await unstage_file(str(workspace), path)
    await session._send_event(AgentEvent(type="diff.git_unstage_file", data={"path": path, "ok": ok}))
    return True


async def handle_diff_git_stage_all(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.diff.git_integration import stage_all

    try:
        workspace = session._resolve_requested_workspace(str(data.get("workspace") or "").strip() or None)
    except ValueError as exc:
        await session._send_event(AgentEvent.error(str(exc), recoverable=True))
        return True
    ok = await stage_all(str(workspace))
    await session._send_event(AgentEvent(type="diff.git_stage_all", data={"ok": ok}))
    return True


async def handle_diff_git_unstage_all(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.diff.git_integration import unstage_all

    try:
        workspace = session._resolve_requested_workspace(str(data.get("workspace") or "").strip() or None)
    except ValueError as exc:
        await session._send_event(AgentEvent.error(str(exc), recoverable=True))
        return True
    ok = await unstage_all(str(workspace))
    await session._send_event(AgentEvent(type="diff.git_unstage_all", data={"ok": ok}))
    return True


async def handle_diff_git_revert_file(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.diff.git_integration import revert_file

    try:
        workspace = session._resolve_requested_workspace(str(data.get("workspace") or "").strip() or None)
        path = session._validate_git_relative_path(str(data.get("path", "")))
    except ValueError as exc:
        await session._send_event(AgentEvent.error(str(exc), recoverable=True))
        return True
    ok = await revert_file(str(workspace), path)
    await session._send_event(AgentEvent(type="diff.git_revert_file", data={"path": path, "ok": ok}))
    return True


HANDLERS: dict[str, Any] = {
    "diff.git_working_tree": handle_diff_git_working_tree,
    "diff.git_staged": handle_diff_git_staged,
    "diff.git_stage_file": handle_diff_git_stage_file,
    "diff.git_unstage_file": handle_diff_git_unstage_file,
    "diff.git_stage_all": handle_diff_git_stage_all,
    "diff.git_unstage_all": handle_diff_git_unstage_all,
    "diff.git_revert_file": handle_diff_git_revert_file,
}
