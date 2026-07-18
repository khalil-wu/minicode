from __future__ import annotations

from typing import TYPE_CHECKING

from backend.ws.command_results import emit_command_error

if TYPE_CHECKING:
    from backend.ws.handler import WebSocketSession


async def handle_diff_git_working_tree(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.diff.git_integration import get_working_tree_diff, get_untracked_files
    from backend.services.diff_service import working_tree_diff_event

    try:
        workspace = session._resolve_requested_workspace(str(data.get("workspace") or "").strip() or None)
    except ValueError as exc:
        await emit_command_error(session, "diff.git_working_tree", exc)
        return True
    result = await get_working_tree_diff(str(workspace))
    untracked = await get_untracked_files(str(workspace))
    await session._send_event(working_tree_diff_event(result, untracked=untracked))
    return True


async def handle_diff_git_staged(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.diff.git_integration import get_staged_diff
    from backend.services.diff_service import staged_diff_event

    try:
        workspace = session._resolve_requested_workspace(str(data.get("workspace") or "").strip() or None)
    except ValueError as exc:
        await emit_command_error(session, "diff.git_staged", exc)
        return True
    result = await get_staged_diff(str(workspace))
    await session._send_event(staged_diff_event(result))
    return True


async def handle_diff_git_stage_file(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.diff.git_integration import stage_file
    from backend.services.diff_service import git_file_action_event

    try:
        workspace = session._resolve_requested_workspace(str(data.get("workspace") or "").strip() or None)
        path = session._validate_git_relative_path(str(data.get("path", "")))
    except ValueError as exc:
        await emit_command_error(session, "diff.git_stage_file", exc)
        return True
    ok = await stage_file(str(workspace), path)
    await session._send_event(git_file_action_event("diff.git_stage_file", path=path, ok=ok))
    return True


async def handle_diff_git_unstage_file(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.diff.git_integration import unstage_file
    from backend.services.diff_service import git_file_action_event

    try:
        workspace = session._resolve_requested_workspace(str(data.get("workspace") or "").strip() or None)
        path = session._validate_git_relative_path(str(data.get("path", "")))
    except ValueError as exc:
        await emit_command_error(session, "diff.git_unstage_file", exc)
        return True
    ok = await unstage_file(str(workspace), path)
    await session._send_event(git_file_action_event("diff.git_unstage_file", path=path, ok=ok))
    return True


async def handle_diff_git_stage_all(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.diff.git_integration import stage_all
    from backend.services.diff_service import git_all_action_event

    try:
        workspace = session._resolve_requested_workspace(str(data.get("workspace") or "").strip() or None)
    except ValueError as exc:
        await emit_command_error(session, "diff.git_stage_all", exc)
        return True
    ok = await stage_all(str(workspace))
    await session._send_event(git_all_action_event("diff.git_stage_all", ok=ok))
    return True


async def handle_diff_git_unstage_all(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.diff.git_integration import unstage_all
    from backend.services.diff_service import git_all_action_event

    try:
        workspace = session._resolve_requested_workspace(str(data.get("workspace") or "").strip() or None)
    except ValueError as exc:
        await emit_command_error(session, "diff.git_unstage_all", exc)
        return True
    ok = await unstage_all(str(workspace))
    await session._send_event(git_all_action_event("diff.git_unstage_all", ok=ok))
    return True


async def handle_diff_git_revert_file(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.diff.git_integration import revert_file
    from backend.services.diff_service import git_file_action_event

    try:
        workspace = session._resolve_requested_workspace(str(data.get("workspace") or "").strip() or None)
        path = session._validate_git_relative_path(str(data.get("path", "")))
    except ValueError as exc:
        await emit_command_error(session, "diff.git_revert_file", exc)
        return True
    ok = await revert_file(str(workspace), path)
    await session._send_event(git_file_action_event("diff.git_revert_file", path=path, ok=ok))
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
