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
    extra: dict[str, Any] | None = None,
) -> None:
    details: dict[str, Any] = {"error_type": "git_command", **(extra or {}), **(identity or {})}
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
    from backend.diff.git_integration import (StructuredDiff, get_working_tree_diff, get_uncommitted_diff, get_untracked_files,
        get_untracked_file_diffs, get_comparison_diff, is_git_worktree)
    from backend.diff.comparison import ComparisonUnavailable, branch_comparison, commit_comparison
    from backend.diff.review import begin_snapshot, prepare_snapshot, repository_state
    from backend.services.diff_service import working_tree_diff_event

    try:
        scope = _resolve_git_scope(session, data)
        source = data.get("source", "unstaged")
        if source not in {"unstaged", "uncommitted", "branch", "commit"}:
            raise ValueError("Unknown working-tree review source")
        if source == "commit" and not data.get("commit_sha"):
            raise ValueError("Commit review requires commit_sha")
    except ValueError as exc:
        await emit_command_error(session, "diff.git_working_tree", exc)
        return True
    identity: dict[str, Any] = {}
    hide_whitespace = data.get("hide_whitespace", False)
    snapshot = begin_snapshot(session, scope.conversation_id, source, hide_whitespace)
    status, reason = "success", None
    try:
        async with _git_execution(session, scope, data, identity) as service:
            is_git_repo = await is_git_worktree(scope.workspace_root, service=service)
            state = await repository_state(scope.workspace_root, service) if is_git_repo else None
            result = StructuredDiff()
            if is_git_repo:
                try:
                    if source == "branch":
                        snapshot.comparison = await branch_comparison(scope.workspace_root, service,
                            base_branch=data.get("base_branch"), merge_base_sha=data.get("merge_base_sha"))
                        result = await get_comparison_diff(scope.workspace_root, [snapshot.comparison["merge_base_sha"]],
                            service=service, hide_whitespace=hide_whitespace, worktree=True)
                    elif source == "commit":
                        snapshot.comparison = await commit_comparison(scope.workspace_root, service, data["commit_sha"])
                        result = await get_comparison_diff(scope.workspace_root,
                            [snapshot.comparison["parent_sha"], snapshot.comparison["commit_sha"]],
                            service=service, hide_whitespace=hide_whitespace)
                    else:
                        read_diff = get_uncommitted_diff if source == "uncommitted" else get_working_tree_diff
                        result = await read_diff(scope.workspace_root, service=service, hide_whitespace=hide_whitespace)
                except ComparisonUnavailable as unavailable:
                    status, reason = "expected", unavailable.reason
            untracked = await get_untracked_files(scope.workspace_root, service=service) if is_git_repo and source != "commit" and status == "success" else []
            untracked_files = await get_untracked_file_diffs(scope.workspace_root, untracked, service=service) if untracked else []
            if source in {"uncommitted", "branch"}:
                # Source i3/t3 combines metadata by path with the real
                # untracked version last; it never shows a second deletion
                # row for the same recreated, now-untracked path.
                untracked_paths = set(untracked)
                result.files = [file for file in result.files if file.path not in untracked_paths or file.status == "U"]
                result.total_additions = sum(file.additions for file in result.files)
                result.total_deletions = sum(file.deletions for file in result.files)
            await prepare_snapshot(snapshot, scope.workspace_root, service, result, untracked_files, repo_state=state)
    except Exception as exc:
        await _emit_git_error(session, "diff.git_working_tree", exc, scope=scope, identity=identity)
        return True
    await _send_scoped_event(session, scope, working_tree_diff_event(result, untracked=untracked, is_git_repo=is_git_repo,
        untracked_files=untracked_files, hide_whitespace=hide_whitespace, snapshot_generation=snapshot.generation,
        source=source, status=status, reason=reason, comparison=snapshot.comparison), identity)
    return True


async def handle_diff_git_staged(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.diff.git_integration import StructuredDiff, get_staged_diff, is_git_worktree
    from backend.services.diff_service import staged_diff_event
    from backend.diff.review import begin_snapshot, prepare_snapshot, repository_state

    try:
        scope = _resolve_git_scope(session, data)
    except ValueError as exc:
        await emit_command_error(session, "diff.git_staged", exc)
        return True
    identity: dict[str, Any] = {}
    hide_whitespace = data.get("hide_whitespace", False)
    snapshot = begin_snapshot(session, scope.conversation_id, "staged", hide_whitespace)
    try:
        async with _git_execution(session, scope, data, identity) as service:
            is_git_repo = await is_git_worktree(scope.workspace_root, service=service)
            state = await repository_state(scope.workspace_root, service) if is_git_repo else None
            result = await get_staged_diff(scope.workspace_root, service=service,
                hide_whitespace=hide_whitespace) if is_git_repo else StructuredDiff()
            await prepare_snapshot(snapshot, scope.workspace_root, service, result, [], repo_state=state)
    except Exception as exc:
        await _emit_git_error(session, "diff.git_staged", exc, scope=scope, identity=identity)
        return True
    await _send_scoped_event(session, scope, staged_diff_event(result, is_git_repo=is_git_repo,
        hide_whitespace=hide_whitespace, snapshot_generation=snapshot.generation), identity)
    return True


async def handle_diff_git_stage_file(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    return await _handle_git_review_action(session, data, command="diff.git_stage_file", action="stage")


async def handle_diff_git_unstage_file(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    return await _handle_git_review_action(session, data, command="diff.git_unstage_file", action="unstage")


async def handle_diff_git_stage_all(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    return await _handle_git_review_action(session, data, command="diff.git_stage_all", action="stage", all_files=True)


async def handle_diff_git_unstage_all(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    return await _handle_git_review_action(session, data, command="diff.git_unstage_all", action="unstage", all_files=True)


async def handle_diff_git_revert_file(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    return await _handle_git_review_action(session, data, command="diff.git_revert_file", action="revert")


async def handle_diff_git_revert_all(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    return await _handle_git_review_action(session, data, command="diff.git_revert_all", action="revert", all_files=True)


async def _handle_git_review_action(session, data, *, command, action, all_files=False):
    from backend.diff.review import StaleGitSnapshot, apply_action, current_snapshot
    from backend.services.diff_service import git_all_action_event, git_file_action_event

    scope = None
    identity: dict[str, Any] = {}
    source, generation = data.get("source"), data.get("snapshot_generation")
    try:
        scope = _resolve_git_scope(session, data, require_conversation=True)
        path = None if all_files else session.validate_git_relative_path(str(data.get("path", "")))
        if action == "revert" and data.get("confirmed") is not True:
            raise ValueError("Reverting discards the displayed changes. Re-send with confirmed=true to proceed.")
        if source not in {"staged", "unstaged"}:
            raise ValueError("Git actions require an explicit staged or unstaged review source")
        if not isinstance(generation, str) or not generation:
            raise ValueError("Git actions require the displayed snapshot_generation")
        snapshot = current_snapshot(session, scope.conversation_id, source, generation)
        async with _git_execution(session, scope, data, identity) as service:
            if snapshot is None:
                raise StaleGitSnapshot()
            result = await apply_action(session, scope.conversation_id, snapshot, scope.workspace_root, service,
                action=action, paths=None if all_files else [path])
    except Exception as error:
        applied_targets = getattr(error, "applied_targets", [])
        status = "partial-success" if applied_targets else "stale-snapshot" if isinstance(error, StaleGitSnapshot) else "error"
        await _emit_git_error(session, command, error, scope=scope, identity=identity,
            extra={"status": status, "ok": False, "source": source, "snapshot_generation": generation,
                "applied_targets": applied_targets})
        return True
    details = {**result, "source": source, "snapshot_generation": generation}
    if result["status"] in {"success", "partial-success"}:
        event = git_all_action_event(command, ok=result["ok"]) if all_files else git_file_action_event(command, path=path, ok=result["ok"])
        event.data.update(details)
        await _send_scoped_event(session, scope, event, identity)
    await session.emit_command_result(command, result.get("message", ""), level="success" if result["ok"] else "error",
        data=scope.apply({**identity, **details}))
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


async def handle_diff_git_file_contents(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.diff.review import MAX_CONTENT_BYTES, StaleGitSnapshot, current_snapshot, file_contents

    scope = None
    identity: dict[str, Any] = {}
    try:
        scope = _resolve_git_scope(session, data, require_conversation=True)
        source, generation = data.get("source"), data.get("snapshot_generation")
        if source not in {"unstaged", "staged", "uncommitted", "branch", "commit"} or not isinstance(generation, str):
            raise ValueError("Git contents require source and snapshot_generation")
        requests = data.get("requests")
        if not isinstance(requests, list) or len(requests) > 4:
            raise ValueError("Git contents requests must contain at most four items")
        snapshot = current_snapshot(session, scope.conversation_id, source, generation)
        async with _git_execution(session, scope, data, identity) as service:
            try:
                if snapshot is None:
                    raise StaleGitSnapshot()
                results = await file_contents(session, scope.conversation_id, snapshot, scope.workspace_root, service, requests,
                    max_object_bytes=data.get("max_object_bytes", MAX_CONTENT_BYTES))
                status = "success"
            except StaleGitSnapshot:
                status = "stale-snapshot"
                results = [{"type": "error", "path": item["path"], "side": item["side"],
                    "error": {"type": "stale-snapshot"}} for item in requests]
        await session.emit_command_result("diff.git_file_contents", "", level="success",
            data=scope.apply({**identity, "status": status, "source": source,
                "snapshot_generation": generation, "results": results}))
    except Exception as exc:
        await _emit_git_error(session, "diff.git_file_contents", exc, scope=scope, identity=identity)
    return True


async def handle_diff_git_review_options(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.diff.comparison import review_options
    from backend.diff.git_integration import is_git_worktree

    scope = None
    identity: dict[str, Any] = {}
    try:
        scope = _resolve_git_scope(session, data, require_conversation=True)
        async with _git_execution(session, scope, data, identity) as service:
            is_git_repo = await is_git_worktree(scope.workspace_root, service=service)
            options = await review_options(scope.workspace_root, service, base_branch=data.get("base_branch")) if is_git_repo else {
                "current_branch": None, "default_base_branch": None, "branches": [], "merge_base_sha": None,
                "commits": [], "comparison_status": "expected", "reason": "repository_unavailable"}
        await session.emit_command_result("diff.git_review_options", "", level="success",
            data=scope.apply({**identity, "status": "success", "is_git_repo": is_git_repo, **options}))
    except Exception as error:
        await _emit_git_error(session, "diff.git_review_options", error, scope=scope, identity=identity)
    return True


HANDLERS: dict[str, Any] = {
    "diff.git_working_tree": handle_diff_git_working_tree,
    "diff.git_staged": handle_diff_git_staged,
    "diff.git_stage_file": handle_diff_git_stage_file,
    "diff.git_unstage_file": handle_diff_git_unstage_file,
    "diff.git_stage_all": handle_diff_git_stage_all,
    "diff.git_unstage_all": handle_diff_git_unstage_all,
    "diff.git_revert_file": handle_diff_git_revert_file,
    "diff.git_revert_all": handle_diff_git_revert_all,
    "diff.git_revert_patch": handle_diff_git_revert_patch,
    "diff.git_file_contents": handle_diff_git_file_contents,
    "diff.git_review_options": handle_diff_git_review_options,
}
