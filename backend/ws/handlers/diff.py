from __future__ import annotations

from typing import Any, TYPE_CHECKING
from contextlib import asynccontextmanager

from backend.ws.command_results import emit_command_error
from backend.ws.command_scope import CommandScope, resolve_command_scope

if TYPE_CHECKING:
    from backend.ws.handler import WebSocketSession


async def _send_scoped_event(
    session: "WebSocketSession",
    scope: CommandScope,
    event: Any,
    identity: dict[str, Any] | None = None,
) -> None:
    event.data.update(identity or {})
    scope.apply(event.data)
    await session.send_event(event)


async def _emit_git_error(
    session: "WebSocketSession",
    command: str,
    error: BaseException,
    *,
    scope: CommandScope | None = None,
    identity: dict[str, Any] | None = None,
) -> None:
    details: dict[str, Any] = {"error_type": "git_command", **(identity or {})}
    if scope is not None:
        scope.apply(details)
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


async def _send_git_action_result(
    session: "WebSocketSession", scope: CommandScope, event: Any, identity: dict[str, Any],
) -> None:
    await _send_scoped_event(session, scope, event, identity)
    await session.emit_command_result(
        event.type, "", level="success", data=scope.apply({"ok": True, **identity}),
    )


def _resolve_git_scope(session, data, *, require_conversation=False):
    return resolve_command_scope(session, data, require_conversation=require_conversation,
        require_workspace=True, allow_executor_uri=True)


@asynccontextmanager
async def _git_execution(session, scope, data, identity):
    from backend.services.workspace_api_service import capture_workspace_execution
    from backend.workspace.executor_editor import ExecutorEditorService
    from backend.workspace.executor_paths import ExecutorPath

    environment_id = data.get("environment_id")
    if environment_id is not None:
        identity["environment_id"] = environment_id
    async with capture_workspace_execution(session, scope.conversation_id, environment_id=environment_id) as context:
        if context is None:
            if scope.workspace_root.startswith("file:"):
                raise ValueError("No usable execution environment is attached to this workspace URI")
            yield None
            return
        async with ExecutorEditorService(context, application_config=session.config) as service:
            identity.update(service.identity())
            identity["cwd_uri"] = service.cwd.uri
            for root in (scope.workspace_root if scope.workspace_root.startswith("file:") else "", data.get("cwd_uri")):
                if root and ExecutorPath.parse(root) != service.cwd:
                    raise ValueError("Command workspace does not match its captured execution owner")
            if not context.turn_environment.is_remote:
                from backend.workspace.trust import is_workspace_trusted

                if not is_workspace_trusted(service.cwd.to_local_path()):
                    raise PermissionError("Workspace folder is not trusted")
            yield service


async def handle_diff_git_working_tree(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.diff.git_integration import StructuredDiff, get_working_tree_diff, get_untracked_files, is_git_worktree
    from backend.services.diff_service import working_tree_diff_event

    try:
        scope = _resolve_git_scope(session, data)
    except ValueError as exc:
        await emit_command_error(session, "diff.git_working_tree", exc)
        return True
    identity: dict[str, Any] = {}
    try:
        async with _git_execution(session, scope, data, identity) as service:
            is_git_repo = await is_git_worktree(scope.workspace_root, service=service)
            result = await get_working_tree_diff(scope.workspace_root, service=service) if is_git_repo else StructuredDiff()
            untracked = await get_untracked_files(scope.workspace_root, service=service) if is_git_repo else []
    except Exception as exc:
        await _emit_git_error(session, "diff.git_working_tree", exc, scope=scope, identity=identity)
        return True
    await _send_scoped_event(session, scope, working_tree_diff_event(result, untracked=untracked, is_git_repo=is_git_repo), identity)
    return True


async def handle_diff_git_staged(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.diff.git_integration import StructuredDiff, get_staged_diff, is_git_worktree
    from backend.services.diff_service import staged_diff_event

    try:
        scope = _resolve_git_scope(session, data)
    except ValueError as exc:
        await emit_command_error(session, "diff.git_staged", exc)
        return True
    identity: dict[str, Any] = {}
    try:
        async with _git_execution(session, scope, data, identity) as service:
            is_git_repo = await is_git_worktree(scope.workspace_root, service=service)
            result = await get_staged_diff(scope.workspace_root, service=service) if is_git_repo else StructuredDiff()
    except Exception as exc:
        await _emit_git_error(session, "diff.git_staged", exc, scope=scope, identity=identity)
        return True
    await _send_scoped_event(session, scope, staged_diff_event(result, is_git_repo=is_git_repo), identity)
    return True


async def handle_diff_git_stage_file(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.diff.git_integration import stage_file
    from backend.services.diff_service import git_file_action_event

    try:
        scope = _resolve_git_scope(session, data)
        path = session.validate_git_relative_path(str(data.get("path", "")))
    except ValueError as exc:
        await emit_command_error(session, "diff.git_stage_file", exc)
        return True
    identity: dict[str, Any] = {}
    try:
        async with _git_execution(session, scope, data, identity) as service:
            ok = await stage_file(scope.workspace_root, path, service=service)
    except Exception as exc:
        await _emit_git_error(session, "diff.git_stage_file", exc, scope=scope, identity=identity)
        return True
    await _send_git_action_result(session, scope, git_file_action_event("diff.git_stage_file", path=path, ok=ok), identity)
    return True


async def handle_diff_git_unstage_file(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.diff.git_integration import unstage_file
    from backend.services.diff_service import git_file_action_event

    try:
        scope = _resolve_git_scope(session, data)
        path = session.validate_git_relative_path(str(data.get("path", "")))
    except ValueError as exc:
        await emit_command_error(session, "diff.git_unstage_file", exc)
        return True
    identity: dict[str, Any] = {}
    try:
        async with _git_execution(session, scope, data, identity) as service:
            ok = await unstage_file(scope.workspace_root, path, service=service)
    except Exception as exc:
        await _emit_git_error(session, "diff.git_unstage_file", exc, scope=scope, identity=identity)
        return True
    await _send_git_action_result(session, scope, git_file_action_event("diff.git_unstage_file", path=path, ok=ok), identity)
    return True


async def handle_diff_git_stage_all(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.diff.git_integration import stage_all
    from backend.services.diff_service import git_all_action_event

    try:
        scope = _resolve_git_scope(session, data)
    except ValueError as exc:
        await emit_command_error(session, "diff.git_stage_all", exc)
        return True
    identity: dict[str, Any] = {}
    try:
        async with _git_execution(session, scope, data, identity) as service:
            ok = await stage_all(scope.workspace_root, service=service)
    except Exception as exc:
        await _emit_git_error(session, "diff.git_stage_all", exc, scope=scope, identity=identity)
        return True
    await _send_git_action_result(session, scope, git_all_action_event("diff.git_stage_all", ok=ok), identity)
    return True


async def handle_diff_git_unstage_all(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.diff.git_integration import unstage_all
    from backend.services.diff_service import git_all_action_event

    try:
        scope = _resolve_git_scope(session, data)
    except ValueError as exc:
        await emit_command_error(session, "diff.git_unstage_all", exc)
        return True
    identity: dict[str, Any] = {}
    try:
        async with _git_execution(session, scope, data, identity) as service:
            ok = await unstage_all(scope.workspace_root, service=service)
    except Exception as exc:
        await _emit_git_error(session, "diff.git_unstage_all", exc, scope=scope, identity=identity)
        return True
    await _send_git_action_result(session, scope, git_all_action_event("diff.git_unstage_all", ok=ok), identity)
    return True


async def handle_diff_git_revert_file(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.diff.git_integration import revert_file
    from backend.services.diff_service import git_file_action_event

    try:
        scope = _resolve_git_scope(session, data)
        path = session.validate_git_relative_path(str(data.get("path", "")))
    except ValueError as exc:
        await emit_command_error(session, "diff.git_revert_file", exc)
        return True
    # git restore --worktree discards uncommitted changes with no undo; the
    # destructive action requires an explicit confirmation round-trip (cc's
    # ExitWorktree refuses destructive deletion without a change count).
    if data.get("confirmed") is not True:
        await emit_command_error(
            session,
            "diff.git_revert_file",
            "Reverting discards all uncommitted changes to this file. Re-send with confirmed=true to proceed.",
            data=scope.apply({}),
        )
        return True
    identity: dict[str, Any] = {}
    try:
        async with _git_execution(session, scope, data, identity) as service:
            ok = await revert_file(scope.workspace_root, path, service=service)
    except Exception as exc:
        await _emit_git_error(session, "diff.git_revert_file", exc, scope=scope, identity=identity)
        return True
    await _send_git_action_result(session, scope, git_file_action_event("diff.git_revert_file", path=path, ok=ok), identity)
    return True


async def handle_diff_git_revert_patch(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.diff.git_integration import revert_patch

    scope = None
    identity: dict[str, Any] = {}
    try:
        scope = _resolve_git_scope(session, data, require_conversation=True)
        patch = data.get("patch")
        if not isinstance(patch, str) or not patch.strip():
            raise ValueError("The displayed patch is required to undo this edit")
        if data.get("confirmed") is not True:
            raise ValueError("Confirm undoing the displayed edit before applying its reverse patch")
        async with _git_execution(session, scope, data, identity) as service:
            await revert_patch(scope.workspace_root, patch, service=service)
    except Exception as exc:
        await _emit_git_error(session, "diff.git_revert_patch", exc, scope=scope, identity=identity)
        return True
    await session.emit_command_result(
        "diff.git_revert_patch", "Displayed changes reverted.", level="success",
        data=scope.apply(identity),
    )
    return True


HANDLERS: dict[str, Any] = {
    "diff.git_working_tree": handle_diff_git_working_tree,
    "diff.git_staged": handle_diff_git_staged,
    "diff.git_stage_file": handle_diff_git_stage_file,
    "diff.git_unstage_file": handle_diff_git_unstage_file,
    "diff.git_stage_all": handle_diff_git_stage_all,
    "diff.git_unstage_all": handle_diff_git_unstage_all,
    "diff.git_revert_file": handle_diff_git_revert_file,
    "diff.git_revert_patch": handle_diff_git_revert_patch,
}
