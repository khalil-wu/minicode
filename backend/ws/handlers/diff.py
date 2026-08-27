from __future__ import annotations

from typing import Any, TYPE_CHECKING

from backend.ws.command_results import emit_command_error
from backend.ws.command_scope import CommandScope, resolve_command_scope

if TYPE_CHECKING:
    from backend.ws.handler import WebSocketSession


async def _send_scoped_event(
    session: "WebSocketSession",
    scope: CommandScope,
    event: Any,
) -> None:
    scope.apply(event.data)
    await session._send_event(event)


async def _emit_git_error(
    session: "WebSocketSession",
    command: str,
    error: BaseException,
) -> None:
    details: dict[str, Any] = {"error_type": "git_command"}
    for source, target in (
        ("exit_code", "exit_code"),
        ("stderr", "stderr"),
        ("cleanup_pending", "cleanup_pending"),
        ("cleanup_reason", "cleanup_reason"),
    ):
        value = getattr(error, source, None)
        if value not in (None, ""):
            details[target] = value
    await emit_command_error(session, command, error, data=details)


async def handle_diff_git_working_tree(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.diff.git_integration import get_working_tree_diff, get_untracked_files
    from backend.services.diff_service import working_tree_diff_event

    try:
        scope = resolve_command_scope(session, data, require_conversation=False)
    except ValueError as exc:
        await emit_command_error(session, "diff.git_working_tree", exc)
        return True
    try:
        result = await get_working_tree_diff(scope.workspace_root)
        untracked = await get_untracked_files(scope.workspace_root)
    except Exception as exc:
        await _emit_git_error(session, "diff.git_working_tree", exc)
        return True
    await _send_scoped_event(session, scope, working_tree_diff_event(result, untracked=untracked))
    return True


async def handle_diff_git_staged(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.diff.git_integration import get_staged_diff
    from backend.services.diff_service import staged_diff_event

    try:
        scope = resolve_command_scope(session, data, require_conversation=False)
    except ValueError as exc:
        await emit_command_error(session, "diff.git_staged", exc)
        return True
    try:
        result = await get_staged_diff(scope.workspace_root)
    except Exception as exc:
        await _emit_git_error(session, "diff.git_staged", exc)
        return True
    await _send_scoped_event(session, scope, staged_diff_event(result))
    return True


async def handle_diff_git_stage_file(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.diff.git_integration import stage_file
    from backend.services.diff_service import git_file_action_event

    try:
        scope = resolve_command_scope(session, data, require_conversation=False)
        path = session._validate_git_relative_path(str(data.get("path", "")))
    except ValueError as exc:
        await emit_command_error(session, "diff.git_stage_file", exc)
        return True
    try:
        ok = await stage_file(scope.workspace_root, path)
    except Exception as exc:
        await _emit_git_error(session, "diff.git_stage_file", exc)
        return True
    await _send_scoped_event(session, scope, git_file_action_event("diff.git_stage_file", path=path, ok=ok))
    return True


async def handle_diff_git_unstage_file(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.diff.git_integration import unstage_file
    from backend.services.diff_service import git_file_action_event

    try:
        scope = resolve_command_scope(session, data, require_conversation=False)
        path = session._validate_git_relative_path(str(data.get("path", "")))
    except ValueError as exc:
        await emit_command_error(session, "diff.git_unstage_file", exc)
        return True
    try:
        ok = await unstage_file(scope.workspace_root, path)
    except Exception as exc:
        await _emit_git_error(session, "diff.git_unstage_file", exc)
        return True
    await _send_scoped_event(session, scope, git_file_action_event("diff.git_unstage_file", path=path, ok=ok))
    return True


async def handle_diff_git_stage_all(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.diff.git_integration import stage_all
    from backend.services.diff_service import git_all_action_event

    try:
        scope = resolve_command_scope(session, data, require_conversation=False)
    except ValueError as exc:
        await emit_command_error(session, "diff.git_stage_all", exc)
        return True
    try:
        ok = await stage_all(scope.workspace_root)
    except Exception as exc:
        await _emit_git_error(session, "diff.git_stage_all", exc)
        return True
    await _send_scoped_event(session, scope, git_all_action_event("diff.git_stage_all", ok=ok))
    return True


async def handle_diff_git_unstage_all(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.diff.git_integration import unstage_all
    from backend.services.diff_service import git_all_action_event

    try:
        scope = resolve_command_scope(session, data, require_conversation=False)
    except ValueError as exc:
        await emit_command_error(session, "diff.git_unstage_all", exc)
        return True
    try:
        ok = await unstage_all(scope.workspace_root)
    except Exception as exc:
        await _emit_git_error(session, "diff.git_unstage_all", exc)
        return True
    await _send_scoped_event(session, scope, git_all_action_event("diff.git_unstage_all", ok=ok))
    return True


async def handle_diff_git_revert_file(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.diff.git_integration import revert_file
    from backend.services.diff_service import git_file_action_event

    try:
        scope = resolve_command_scope(session, data, require_conversation=False)
        path = session._validate_git_relative_path(str(data.get("path", "")))
    except ValueError as exc:
        await emit_command_error(session, "diff.git_revert_file", exc)
        return True
    # git restore --worktree discards uncommitted changes with no undo; the
    # destructive action requires an explicit confirmation round-trip (cc's
    # ExitWorktree refuses destructive deletion without a change count).
    confirmed = bool(data.get("confirmed", False))
    if not confirmed:
        await emit_command_error(
            session,
            "diff.git_revert_file",
            "Reverting discards all uncommitted changes to this file. Re-send with confirmed=true to proceed.",
        )
        return True
    try:
        ok = await revert_file(scope.workspace_root, path)
    except Exception as exc:
        await _emit_git_error(session, "diff.git_revert_file", exc)
        return True
    await _send_scoped_event(session, scope, git_file_action_event("diff.git_revert_file", path=path, ok=ok))
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
