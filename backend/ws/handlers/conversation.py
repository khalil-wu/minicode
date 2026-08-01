from __future__ import annotations

import asyncio
import copy
import json
import logging
from contextlib import suppress
from pathlib import Path
from typing import Any, TYPE_CHECKING

from backend.ws.conversation_errors import emit_conversation_not_found
if TYPE_CHECKING:
    from backend.ws.handler import WebSocketSession

logger = logging.getLogger(__name__)


async def _stop_conversation_run(session: "WebSocketSession", conversation_id: str, *, reason: str) -> bool:
    """Cancel a run and report whether its lifecycle actually converged."""
    session._run_manager.clear_user_message_queue(conversation_id)
    task = session._running_agent_task_for(conversation_id)
    if task is None:
        return True
    await session._cancel_agent_runs(conversation_id=conversation_id, reason=reason)
    # RunManager.cancel performs the shared bounded drain.  Never follow it
    # with an unbounded await: a cancellation-resistant tool must block the
    # destructive operation, not the websocket forever.
    if task.done():
        with suppress(asyncio.CancelledError, Exception):
            task.result()
        return True
    return False


async def handle_conversation_create(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.conversation_payload_service import parse_conversation_create_request

    request = parse_conversation_create_request(data)
    if request.workspace_required_error is not None:
        from backend.ws.command_results import emit_command_error
        await emit_command_error(session, "conversation.create", request.workspace_required_error)
        return True
    base_summary, snapshot, inherited_facts = session._build_inherited_snapshot(request.memory_mode)
    created = session.conversation_repo.create_conversation(
        conversation_id=request.conversation_id,
        title=request.title,
        memory_mode=request.memory_mode,
        permission_mode=request.permission_mode,
        summary=base_summary,
        inherited_facts=inherited_facts,
        context_snapshot=snapshot,
        workspace_root=request.workspace_root,
        git_isolated=request.git_isolated,
    )
    if request.git_isolated:
        created = await session._create_isolated_conversation_worktree(created) or created
    elif request.workspace_root:
        created = session.conversation_repo.update_workspace_binding(
            created.id,
            workspace_root=request.workspace_root,
            git_branch=session._git_branch_for(Path(request.workspace_root)),
            worktree_path="",
            git_isolated=False,
        ) or created
    if request.activate:
        session.active_conversation_id = created.id
    if request.activate:
        if request.workspace_root:
            await session._switch_workspace_for_conversation(created, announce=False)
        else:
            clear_runtime = getattr(session, "_clear_workspace_runtime", None)
            if callable(clear_runtime):
                clear_runtime()
    if request.activate:
        session._load_active_conversation_snapshot(created.id, created.context_snapshot)
        session._sync_permission_mode_with_active_conversation(source="conversation.create")
    await session._send_conversation_list()
    return True


async def handle_conversation_clone(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    """Clone a persisted session without sharing mutable state or worktree ownership."""
    source_id = str(data.get("conversation_id") or session.active_conversation_id or "").strip()
    source = session.conversation_repo.get_conversation(source_id) if source_id else None
    if source is None:
        await emit_conversation_not_found(session, source_id)
        return True
    if session._running_agent_task_for(source.id) is not None:
        await session._emit_command_result(
            "conversation.clone",
            "Cannot clone a conversation while its agent run is active.",
            level="warning",
            data={"conversation_id": source.id, "reason": "run_active"},
        )
        return True
    title = str(data.get("title") or "").strip() or None
    clone = session.conversation_repo.clone_conversation(source.id, title=title)
    if clone is None:
        await emit_conversation_not_found(session, source.id)
        return True
    if bool(data.get("activate")):
        session.active_conversation_id = clone.id
        await session._switch_workspace_for_conversation(clone, announce=False)
        session._load_active_conversation_snapshot(clone.id, clone.context_snapshot)
        session._sync_permission_mode_with_active_conversation(source="conversation.clone")
    await session._send_conversation_list()
    await session._emit_command_result(
        "conversation.clone",
        f"Cloned conversation as {clone.title}.",
        level="success",
        data={
            "conversation_id": clone.id,
            "source_conversation_id": source.id,
            "branch_kind": clone.branch_kind,
            "activated": bool(data.get("activate")),
        },
    )
    return True


async def handle_conversation_merge(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    """Fast-forward a branch into its direct parent, with explicit conflicts."""
    source_id = str(data.get("conversation_id") or session.active_conversation_id or "").strip()
    source = session.conversation_repo.get_conversation(source_id) if source_id else None
    if source is None:
        await emit_conversation_not_found(session, source_id)
        return True
    target_id = str(data.get("target_conversation_id") or source.parent_conversation_id or "").strip()
    target = session.conversation_repo.get_conversation(target_id) if target_id else None
    if target is None:
        await emit_conversation_not_found(session, target_id)
        return True
    if session._running_agent_task_for(source.id) is not None or session._running_agent_task_for(target.id) is not None:
        await session._emit_command_result(
            "conversation.merge",
            "Stop both agent runs before merging sessions.",
            level="warning",
            data={"conversation_id": source.id, "target_conversation_id": target.id, "reason": "run_active"},
        )
        return True
    _, updated_target, status = session.conversation_repo.merge_conversation_fast_forward(source.id, target.id)
    messages = {
        "merged": "Branch merged into its parent.",
        "already_up_to_date": "The parent already contains this branch.",
        "target_diverged": "Merge stopped: the parent changed after the branch was created.",
        "source_is_not_direct_child": "Merge stopped: only a direct child can be merged into its parent.",
        "already_merged_elsewhere": "Merge stopped: this branch was already merged elsewhere.",
        "archived_conversation": "Merge stopped: archived sessions cannot be merged.",
        "same_conversation": "A session cannot be merged into itself.",
        "conversation_not_found": "Merge stopped: a session no longer exists.",
    }
    level = "success" if status in {"merged", "already_up_to_date"} else "warning"
    active_target_merged = status == "merged" and updated_target is not None and session.active_conversation_id == updated_target.id
    if active_target_merged and updated_target is not None:
        from backend.services.conversation_payload_service import build_conversation_switched_payload

        is_hydrating = session._load_active_conversation_snapshot(
            updated_target.id,
            updated_target.context_snapshot,
            notify=True,
        )
        await session._send_ws_payload(
            build_conversation_switched_payload(
                updated_target,
                is_hydrating=is_hydrating,
                runtime_snapshot=session.runtime_snapshot(),
            ),
            log_context="conversation.switched",
        )
    if status in {"merged", "already_up_to_date"}:
        await session._send_conversation_list()
    await session._emit_command_result(
        "conversation.merge",
        messages.get(status, f"Merge stopped: {status}"),
        level=level,
        data={
            "conversation_id": source.id,
            "target_conversation_id": target.id,
            "status": status,
        },
    )
    return True


async def handle_conversation_export(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    source_id = str(data.get("conversation_id") or session.active_conversation_id or "").strip()
    if not source_id or session.conversation_repo.get_conversation(source_id) is None:
        await emit_conversation_not_found(session, source_id)
        return True
    include_descendants = bool(data.get("include_descendants", True))
    payload = session.conversation_repo.export_conversation_tree(
        source_id,
        include_descendants=include_descendants,
    )
    if payload is None:
        await emit_conversation_not_found(session, source_id)
        return True
    content = json.dumps(payload, ensure_ascii=False, indent=2)
    # Keep a single export from monopolizing the WebSocket replay buffer.
    if len(content.encode("utf-8")) > 25 * 1024 * 1024:
        await session._emit_command_result(
            "conversation.export",
            "Export is larger than 25 MiB; narrow the tree and try again.",
            level="warning",
            data={"conversation_id": source_id, "reason": "export_too_large"},
        )
        return True
    safe_id = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in source_id)
    await session._emit_command_result(
        "conversation.export",
        "Conversation export is ready to download.",
        level="success",
        data={
            "conversation_id": source_id,
            "filename": f"minicode-{safe_id}.json",
            "mime_type": "application/json;charset=utf-8",
            "content": content,
            "conversation_count": len(payload.get("conversations") or []),
        },
    )
    return True


async def handle_conversation_switch(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.conversation_payload_service import build_conversation_switched_payload

    conversation_id = str(data.get("conversation_id", ""))
    target = session.conversation_repo.get_conversation(conversation_id)
    if target is None:
        await emit_conversation_not_found(session, conversation_id)
        return True
    if getattr(target, "archived", False):
        await session._send_conversation_list()
        return True
    session.active_conversation_id = target.id
    await session._switch_workspace_for_conversation(target, announce=True)
    is_hydrating = session._load_active_conversation_snapshot(target.id, target.context_snapshot, notify=True)
    session._sync_permission_mode_with_active_conversation(source="conversation.switch")
    logger.info(
        "[handle_conversation_switch] Switch to conv: %s, transcript len: %d, snapshot keys: %s",
        target.id,
        len(target.transcript) if target.transcript else 0,
        list(target.context_snapshot.keys()) if target.context_snapshot else [],
    )
    await session._send_ws_payload(
        build_conversation_switched_payload(
            target,
            is_hydrating=is_hydrating,
            runtime_snapshot=session.runtime_snapshot(),
        ),
        log_context="conversation.switched",
    )
    if data.get("_reemit_pending", True):
        await session._reemit_pending_state(conversation_id=target.id)
    return True


def _clear_active_conversation_runtime(session: "WebSocketSession") -> None:
    session.active_conversation_id = None
    try:
        session.context_builder.clear()
    except Exception:
        pass
    clear_runtime = getattr(session, "_clear_workspace_runtime", None)
    if callable(clear_runtime):
        clear_runtime()


async def _activate_conversation_or_blank(session: "WebSocketSession", preferred_id: str | None = None) -> None:
    from backend.services.conversation_payload_service import choose_conversation_activation_target

    target = choose_conversation_activation_target(session.conversation_repo, preferred_id)
    if target is None:
        _clear_active_conversation_runtime(session)
        return

    session.active_conversation_id = target.id
    await session._switch_workspace_for_conversation(target, announce=True)
    session._load_active_conversation_snapshot(target.id, target.context_snapshot)
    session._sync_permission_mode_with_active_conversation(source="conversation.activate")


async def handle_conversation_list(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    preferred = str(data.get("preferred_conversation_id") or "").strip()
    if preferred:
        await _activate_conversation_or_blank(session, preferred)
    elif session.active_conversation_id:
        active = session.conversation_repo.get_conversation(session.active_conversation_id)
        if active is None or getattr(active, "archived", False):
            await _activate_conversation_or_blank(session)
    await session._send_conversation_list()
    return True


async def handle_conversation_rename(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.conversation_payload_service import parse_conversation_rename_request

    request = parse_conversation_rename_request(data)
    updated = session.conversation_repo.rename_conversation(request.conversation_id, request.title)
    if updated is None:
        await emit_conversation_not_found(session, request.conversation_id)
        return True
    await session._send_conversation_list()
    return True


async def handle_conversation_archive(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    conversation_id = str(data.get("conversation_id", ""))
    updated = session.conversation_repo.set_archived(conversation_id, True)
    if updated is None:
        await emit_conversation_not_found(session, conversation_id)
        return True
    if session.active_conversation_id == conversation_id:
        await _activate_conversation_or_blank(session)
    await session._send_conversation_list()
    return True


async def handle_conversation_unarchive(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    conversation_id = str(data.get("conversation_id", ""))
    updated = session.conversation_repo.set_archived(conversation_id, False)
    if updated is None:
        await emit_conversation_not_found(session, conversation_id)
        return True
    await session._send_conversation_list()
    return True


async def handle_conversation_delete(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.conversation_payload_service import (
        build_worktree_cleanup_outcome,
        build_worktree_cleanup_force_required_outcome,
        parse_conversation_delete_request,
    )

    request = parse_conversation_delete_request(data)
    target = session.conversation_repo.get_conversation(request.conversation_id)
    if target is None:
        await emit_conversation_not_found(session, request.conversation_id)
        return True

    stopped = await _stop_conversation_run(
        session,
        request.conversation_id,
        reason="conversation_deleted",
    )
    if not stopped:
        await session._emit_command_result(
            "conversation.delete",
            "The agent run did not stop within the lifecycle deadline; the conversation and worktree were kept intact.",
            level="error",
            data={"conversation_id": request.conversation_id, "reason": "run_still_active"},
        )
        return True

    # Release resources that can still hold or write into the workspace before
    # taking the recoverable worktree snapshot and removing the checkout.
    await session.terminal_manager.destroy_sessions_for_conversation(request.conversation_id)
    from backend.preview import stop_preview_launch

    await stop_preview_launch(
        session_id=session.session_id,
        conversation_id=request.conversation_id,
    )

    released_active_workspace = False
    if request.cleanup_worktree and session.active_conversation_id == request.conversation_id:
        clear_runtime = getattr(session, "_clear_workspace_runtime", None)
        if callable(clear_runtime):
            clear_runtime()
            released_active_workspace = True

    if request.cleanup_worktree:
        cleanup = await _cleanup_conversation_worktree(session, target, force=request.force_cleanup)
        if not cleanup.get("removed"):
            if released_active_workspace:
                await session._switch_workspace_for_conversation(target, announce=False)
            outcome = (
                build_worktree_cleanup_force_required_outcome(cleanup)
                if cleanup.get("needs_force")
                else build_worktree_cleanup_outcome(cleanup)
            )
            await session._emit_command_result(
                outcome.command,
                outcome.message,
                level=outcome.level,
                data=outcome.data,
            )
            return True
    deleted = session.conversation_repo.delete_conversation(request.conversation_id)
    if not deleted:
        await emit_conversation_not_found(session, request.conversation_id)
        return True
    if session.active_conversation_id == request.conversation_id:
        await _activate_conversation_or_blank(session)
    await session._send_conversation_list()
    return True


async def handle_conversation_worktree_cleanup(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    conversation_id = str(data.get("conversation_id", "")).strip()
    target = session.conversation_repo.get_conversation(conversation_id)
    if target is None:
        await emit_conversation_not_found(session, conversation_id)
        return True

    from backend.services.conversation_payload_service import build_worktree_cleanup_outcome

    cleanup = await _cleanup_conversation_worktree(session, target, force=bool(data.get("force")))
    outcome = build_worktree_cleanup_outcome(cleanup)
    await session._emit_command_result(
        outcome.command,
        outcome.message,
        level=outcome.level,
        data=outcome.data,
    )
    if cleanup.get("removed"):
        main_workspace_root = str(cleanup.get("workspace_root") or "").strip()
        updated = session.conversation_repo.update_workspace_binding(
            target.id,
            workspace_root=main_workspace_root,
            git_branch="",
            worktree_path="",
            git_isolated=False,
        )
        if updated is not None:
            await session._send_conversation_list()
    return True


async def handle_conversation_worktree_handoff_preflight(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.conversation_worktree_handoff_service import build_handoff_preflight

    conversation_id = str(data.get("conversation_id") or "").strip()
    target = session.conversation_repo.get_conversation(conversation_id)
    if target is None:
        await emit_conversation_not_found(session, conversation_id)
        return True
    preflight = await asyncio.to_thread(
        build_handoff_preflight,
        target,
        target=str(data.get("target") or ("local" if getattr(target, "git_isolated", False) else "worktree")),
        conversation_repo=session.conversation_repo,
        main_worktree_root=session._main_worktree_root,
        has_running_turn=session._running_agent_task_for(conversation_id) is not None,
        dirty_action=str(data.get("dirty_action") or "block"),
    )
    await session._emit_command_result(
        "conversation.worktree.handoff.preflight",
        "Workspace handoff is ready." if preflight["allowed"] else "Workspace handoff is blocked.",
        level="success" if preflight["allowed"] else "warning",
        data=preflight,
    )
    return True


async def handle_conversation_worktree_handoff_execute(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.conversation_payload_service import create_isolated_worktree_binding
    from backend.services.conversation_worktree_handoff_service import (
        build_handoff_preflight,
        restore_workspace_stash,
        stash_workspace_changes,
        switch_main_checkout,
    )
    from backend.workspace.worktree import WorktreeManager

    conversation_id = str(data.get("conversation_id") or "").strip()
    conversation = session.conversation_repo.get_conversation(conversation_id)
    if conversation is None:
        await emit_conversation_not_found(session, conversation_id)
        return True
    target_kind = str(data.get("target") or ("local" if getattr(conversation, "git_isolated", False) else "worktree"))
    dirty_action = str(data.get("dirty_action") or "block")
    preflight = await asyncio.to_thread(
        build_handoff_preflight,
        conversation,
        target=target_kind,
        conversation_repo=session.conversation_repo,
        main_worktree_root=session._main_worktree_root,
        has_running_turn=session._running_agent_task_for(conversation_id) is not None,
        dirty_action=dirty_action,
    )
    if not preflight["allowed"] or str(data.get("fingerprint") or "") != preflight["fingerprint"]:
        await session._emit_command_result(
            "conversation.worktree.handoff.execute",
            "Workspace changed after preflight; review the checks and try again.",
            level="warning",
            data={**preflight, "stale": True},
        )
        return True

    source_path = Path(str(getattr(conversation, "worktree_path", "") or getattr(conversation, "workspace_root", "") or ".")).resolve()
    stash_ref = ""
    if dirty_action == "stash":
        stashed, stash_ref = await asyncio.to_thread(
            stash_workspace_changes,
            source_path,
            label=f"minicode-handoff-{conversation_id}",
        )
        if not stashed:
            await session._emit_command_result(
                "conversation.worktree.handoff.execute",
                f"Could not safely stash local changes: {stash_ref}",
                level="error",
                data=preflight,
            )
            return True

    if target_kind == "worktree":
        creation = await asyncio.to_thread(
            create_isolated_worktree_binding,
            conversation,
            current_workspace_root=session._current_workspace_root(),
            main_worktree_root=session._main_worktree_root,
        )
        if not creation.created:
            await session._emit_command_result("conversation.worktree.handoff.execute", "Failed to create protected workspace.", level="error", data=preflight)
            return True
        if stash_ref:
            restored, error = await asyncio.to_thread(
                restore_workspace_stash,
                Path(creation.workspace_root),
                stash_ref,
            )
            if not restored:
                await session._emit_command_result(
                    "conversation.worktree.handoff.execute",
                    "The new worktree was created, but restoring stashed changes conflicted. The stash is retained for recovery.",
                    level="error",
                    data={**preflight, "stash_ref": stash_ref, "stash_error": error, "workspace_root": creation.workspace_root},
                )
                return True
        updated = await asyncio.to_thread(
            session.conversation_repo.update_workspace_binding,
            conversation_id,
            workspace_root=creation.workspace_root,
            git_branch=creation.git_branch,
            worktree_path=creation.worktree_path,
            git_isolated=True,
        )
    else:
        base_root = session._main_worktree_root(source_path)
        branch = str(getattr(conversation, "git_branch", "") or "").strip()
        if session.active_conversation_id == conversation_id:
            clear_runtime = getattr(session, "_clear_workspace_runtime", None)
            if callable(clear_runtime):
                clear_runtime()
        manager = WorktreeManager(base_root)
        if not await asyncio.to_thread(manager.remove_worktree, source_path, force=False):
            await session._emit_command_result("conversation.worktree.handoff.execute", "Failed to remove the protected workspace.", level="error", data=preflight)
            return True
        switched, error = await asyncio.to_thread(switch_main_checkout, base_root, branch)
        if not switched:
            await asyncio.to_thread(
                manager.create_worktree,
                source_path,
                branch=branch,
                new_branch=False,
            )
            if stash_ref:
                await asyncio.to_thread(restore_workspace_stash, source_path, stash_ref)
            await session._emit_command_result("conversation.worktree.handoff.execute", f"Failed to switch the local checkout: {error}", level="error", data=preflight)
            return True
        if stash_ref:
            restored, error = await asyncio.to_thread(
                restore_workspace_stash,
                base_root,
                stash_ref,
            )
            if not restored:
                await session._emit_command_result(
                    "conversation.worktree.handoff.execute",
                    "Local checkout switched, but restoring stashed changes conflicted. The stash is retained for recovery.",
                    level="error",
                    data={**preflight, "stash_ref": stash_ref, "stash_error": error, "workspace_root": str(base_root)},
                )
                return True
        updated = await asyncio.to_thread(
            session.conversation_repo.update_workspace_binding,
            conversation_id,
            workspace_root=str(base_root),
            git_branch=branch,
            worktree_path="",
            git_isolated=False,
        )

    if updated is None:
        await session._emit_command_result("conversation.worktree.handoff.execute", "Failed to update the conversation workspace binding.", level="error", data=preflight)
        return True
    if session.active_conversation_id == conversation_id:
        await session._switch_workspace_for_conversation(updated, announce=True)
    await session._send_conversation_list()
    await session._emit_command_result(
        "conversation.worktree.handoff.execute",
        "Moved task to protected workspace." if target_kind == "worktree" else "Moved task to local checkout.",
        data={**preflight, "completed": True, "workspace_root": str(getattr(updated, "workspace_root", "") or ""), "worktree_path": str(getattr(updated, "worktree_path", "") or ""), "git_branch": str(getattr(updated, "git_branch", "") or ""), "git_isolated": bool(getattr(updated, "git_isolated", False))},
    )
    return True


async def handle_conversation_clear(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.conversation_payload_service import (
        build_conversation_clear_outcome,
        build_conversation_switched_payload,
        parse_conversation_clear_request,
    )

    request = parse_conversation_clear_request(data, active_conversation_id=str(session.active_conversation_id or ""))
    if not request.conversation_id:
        return False
    target = session.conversation_repo.get_conversation(request.conversation_id)
    if target is None:
        return False
    stopped = await _stop_conversation_run(session, request.conversation_id, reason="conversation_cleared")
    if not stopped:
        await session._emit_command_result(
            "clear",
            "The agent run did not stop within the lifecycle deadline; conversation history was not cleared.",
            level="error",
            data={"conversation_id": request.conversation_id, "reason": "run_still_active"},
        )
        return True
    session.conversation_repo.replace_transcript(request.conversation_id, [])
    session.conversation_repo.update_summary(request.conversation_id, "")
    session.conversation_repo.update_facts(request.conversation_id, local_facts=[])
    session.conversation_repo.update_compaction(request.conversation_id, "clean", "")
    session.conversation_repo.save_context_snapshot(request.conversation_id, {})
    if request.conversation_id == session.active_conversation_id:
        session.context_builder.clear()
        session._load_active_conversation_snapshot(request.conversation_id, {})
    await session._send_conversation_list()
    if request.conversation_id == session.active_conversation_id:
        await session._send_ws_payload(
            build_conversation_switched_payload(
                session.conversation_repo.get_conversation(request.conversation_id),
                is_hydrating=False,
                runtime_snapshot=session.runtime_snapshot(),
            ),
            log_context="conversation.switched",
        )
    outcome = build_conversation_clear_outcome()
    await session._emit_command_result(outcome.command, outcome.message, level=outcome.level, data=outcome.data)
    return True


async def handle_conversation_truncate(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.conversation_payload_service import (
        build_conversation_truncate_failed_outcome,
        build_conversation_truncated_outcome,
        parse_conversation_truncate_request,
    )

    request = parse_conversation_truncate_request(data, active_conversation_id=str(session.active_conversation_id or ""))
    if request.error is not None:
        outcome = request.error
        await session._emit_command_result(
            outcome.command,
            outcome.message,
            level=outcome.level,
            data=outcome.data,
        )
        return True

    target = session.conversation_repo.get_conversation(request.conversation_id)
    if target is None:
        await emit_conversation_not_found(session, request.conversation_id)
        return True

    updated = session.conversation_runtime.rewind_to_user_turn(
        conversation=target,
        retry_from_message_id=request.message_id,
    )
    if updated is None:
        outcome = build_conversation_truncate_failed_outcome(
            conversation_id=request.conversation_id,
            message_id=request.message_id,
        )
        await session._emit_command_result(
            outcome.command,
            outcome.message,
            level=outcome.level,
            data=outcome.data,
        )
        return True

    if request.conversation_id == session.active_conversation_id:
        session._load_active_conversation_snapshot(updated.id, updated.context_snapshot)
        session._sync_permission_mode_with_active_conversation(source="conversation.truncate")

    await session._send_conversation_list()
    outcome = build_conversation_truncated_outcome(updated, message_id=request.message_id)
    await session._emit_command_result(
        outcome.command,
        outcome.message,
        level=outcome.level,
        data=outcome.data,
    )
    return True


async def handle_conversation_memory_mode_set(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.conversation_payload_service import parse_conversation_memory_mode_request

    request = parse_conversation_memory_mode_request(data, active_conversation_id=str(session.active_conversation_id or ""))
    updated = session.conversation_repo.update_memory_mode(request.conversation_id, request.memory_mode)
    if updated is not None:
        await session._send_conversation_list()
    return True


async def handle_conversation_permission_mode_set(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.conversation_permission_service import plan_permission_mode_update

    plan = plan_permission_mode_update(data, active_conversation_id=str(session.active_conversation_id or ""))
    if plan.error_event is not None:
        from backend.ws.command_results import emit_command_error
        await emit_command_error(session, "conversation.permission_mode.set", plan.error_event)
        return True

    if plan.session_only:
        if session._set_permission_context_mode(plan.requested, source=plan.source):
            await session._emit_permission_mode_updated()
            await session._send_task_runtime_update()
        await session._send_conversation_list()
        return True

    updated = session.conversation_repo.update_permission_mode(plan.conversation_id, plan.requested)
    if updated is None:
        await emit_conversation_not_found(session, plan.conversation_id)
        return True

    if plan.conversation_id == session.active_conversation_id:
        session._set_permission_context_mode(plan.requested, source=plan.source)
        await session._emit_permission_mode_updated()
        if plan.auto_approve_reason:
            await session._auto_approve_pending_tool_approvals(
                reason=plan.auto_approve_reason,
                conversation_id=plan.conversation_id,
                only_auto_allowed=plan.only_auto_allowed,
            )
        await session._send_task_runtime_update()

    await session._send_conversation_list()
    return True


async def _emit_goal_updated(
    session: "WebSocketSession",
    *,
    conversation_id: str,
    goal: dict[str, Any],
    source: str,
) -> None:
    from backend.services.conversation_goal_service import build_goal_updated_payload

    await session._send_ws_payload(
        build_goal_updated_payload(conversation_id=conversation_id, goal=goal, source=source),
        log_context="goal.updated",
    )


async def handle_conversation_goal_set(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.conversation_goal_service import prepare_goal_action, resolve_goal_target

    conversation_id, target = resolve_goal_target(
        session.conversation_repo,
        data,
        active_conversation_id=str(session.active_conversation_id or ""),
    )
    source = str(data.get("source") or "websocket.command").strip() or "websocket.command"
    if not conversation_id:
        await session._emit_command_result("goal", "No active conversation to update.", level="warning")
        return True
    if target is None:
        await session._emit_command_result("goal", f"Conversation '{conversation_id}' not found.", level="error")
        return True

    current_goal = dict(getattr(target, "goal", {}) or {})
    action = prepare_goal_action(
        data,
        conversation_id=conversation_id,
        current_goal=current_goal,
        source=source,
    )
    if not action.should_update and (
        action.event_scope == "always"
        or (action.event_scope == "active" and conversation_id == session.active_conversation_id)
    ):
        await _emit_goal_updated(
            session,
            conversation_id=conversation_id,
            goal=action.event_goal,
            source=source,
        )

    if action.should_update:
        updated = session.conversation_repo.update_goal(conversation_id, action.next_goal)
        if updated is None:
            await session._emit_command_result("goal", f"Conversation '{conversation_id}' not found.", level="error")
            return True
        if action.event_scope == "active" and conversation_id == session.active_conversation_id:
            await _emit_goal_updated(
                session,
                conversation_id=conversation_id,
                goal=action.event_goal,
                source=source,
            )
        await session._send_conversation_list()

    await session._emit_command_result(
        action.outcome.command,
        action.outcome.message,
        level=action.outcome.level,
        data=action.outcome.data,
    )
    return True


async def handle_conversation_permission_rules_list(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.permission_rules_service import build_permission_rules_list_outcome, resolve_permission_rule_target

    conversation_id, target = resolve_permission_rule_target(
        session.conversation_repo,
        data,
        active_conversation_id=str(session.active_conversation_id or ""),
    )
    if not conversation_id:
        await session._emit_command_result("permissions.rules.list", "No active conversation to inspect", level="warning")
        return True
    if target is None:
        await session._emit_command_result("permissions.rules.list", f"Conversation '{conversation_id}' not found", level="error")
        return True

    source = str(data.get("source") or "websocket.command").strip() or "websocket.command"
    await session._emit_permission_rules_updated(conversation_id=conversation_id, source=source)
    rules = session._build_permission_rules_payload(conversation=target)
    outcome = build_permission_rules_list_outcome(conversation_id, rules)
    await session._emit_command_result(
        outcome.command,
        outcome.message,
        data=outcome.data,
    )
    return True


async def handle_conversation_permission_rules_add(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.permission_rules_service import prepare_permission_rule_add, resolve_permission_rule_target

    conversation_id, target = resolve_permission_rule_target(
        session.conversation_repo,
        data,
        active_conversation_id=str(session.active_conversation_id or ""),
    )
    if not conversation_id:
        await session._emit_command_result("permissions.rules.add", "No active conversation to update", level="warning")
        return True
    if target is None:
        await session._emit_command_result("permissions.rules.add", f"Conversation '{conversation_id}' not found", level="error")
        return True

    mutation = prepare_permission_rule_add(target, data, conversation_id=conversation_id)
    if not mutation.should_update:
        await session._emit_command_result(
            mutation.outcome.command,
            mutation.outcome.message,
            level=mutation.outcome.level,
            data=mutation.outcome.data,
        )
        return True

    updated = session.conversation_repo.update_permission_rules(
        conversation_id, deny_rules=mutation.deny_rules, overrides=mutation.serialized_overrides,
    )
    if updated is None:
        await session._emit_command_result("permissions.rules.add", f"Conversation '{conversation_id}' not found", level="error")
        return True

    source = str(data.get("source") or "websocket.command").strip() or "websocket.command"
    if conversation_id == session.active_conversation_id:
        session._set_permission_context_rules(
            session_overrides=mutation.overrides,
            tool_deny_rules=mutation.deny_rules,
            source=source,
        )
        await session._send_task_runtime_update()

    await session._emit_permission_rules_updated(conversation_id=conversation_id, source=source)
    await session._emit_command_result(
        mutation.outcome.command,
        mutation.outcome.message,
        level=mutation.outcome.level,
        data=mutation.outcome.data,
    )
    return True


async def handle_conversation_permission_rules_remove(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.permission_rules_service import prepare_permission_rule_remove, resolve_permission_rule_target

    conversation_id, target = resolve_permission_rule_target(
        session.conversation_repo,
        data,
        active_conversation_id=str(session.active_conversation_id or ""),
    )
    if not conversation_id:
        await session._emit_command_result("permissions.rules.remove", "No active conversation to update", level="warning")
        return True
    if target is None:
        await session._emit_command_result("permissions.rules.remove", f"Conversation '{conversation_id}' not found", level="error")
        return True

    mutation = prepare_permission_rule_remove(target, data, conversation_id=conversation_id)
    if not mutation.should_update:
        await session._emit_command_result(
            mutation.outcome.command,
            mutation.outcome.message,
            level=mutation.outcome.level,
            data=mutation.outcome.data,
        )
        return True

    updated = session.conversation_repo.update_permission_rules(
        conversation_id, deny_rules=mutation.deny_rules, overrides=mutation.serialized_overrides,
    )
    if updated is None:
        await session._emit_command_result("permissions.rules.remove", f"Conversation '{conversation_id}' not found", level="error")
        return True

    source = str(data.get("source") or "websocket.command").strip() or "websocket.command"
    if conversation_id == session.active_conversation_id:
        session._set_permission_context_rules(
            session_overrides=mutation.overrides,
            tool_deny_rules=mutation.deny_rules,
            source=source,
        )
        await session._send_task_runtime_update()

    await session._emit_permission_rules_updated(conversation_id=conversation_id, source=source)
    await session._emit_command_result(
        mutation.outcome.command,
        mutation.outcome.message,
        level=mutation.outcome.level,
        data=mutation.outcome.data,
    )
    return True


async def _cleanup_conversation_worktree(session: "WebSocketSession", conversation: Any, *, force: bool = False) -> dict[str, Any]:
    from backend.services.conversation_payload_service import cleanup_isolated_worktree

    return cleanup_isolated_worktree(
        conversation,
        force=force,
        current_workspace_root=session._current_workspace_root(),
        main_worktree_root=session._main_worktree_root,
        is_path_within=session._is_path_within,
        worktree_has_local_changes=session._worktree_has_local_changes,
    )


async def handle_permissions_content_rule_add(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    """Persist a global Tool(content) permission rule (settings.json).

    Drives the approval dialog's "always allow/deny this" action. The runtime
    PermissionChecker is rebuilt from load_config() on each tool call, so the
    saved rule takes effect immediately for subsequent calls.
    """
    from backend.config import SETTINGS_FILE
    from backend.hooks.runtime import run_config_change_hook
    from backend.services.permission_content_service import add_permission_content_rule

    result = add_permission_content_rule(str(data.get("rule") or ""), deny=bool(data.get("deny", False)))
    if result.should_emit_config_change:
        await run_config_change_hook(source="permissions", file_path=str(SETTINGS_FILE))
    outcome = result.outcome
    await session._emit_command_result(
        outcome.command,
        outcome.message,
        level=outcome.level,
        data=outcome.data,
    )
    return True


async def handle_context_compact(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    """Manually trigger context compaction with an optional focus string."""
    focus = str(data.get("focus") or "").strip()
    ctx = session.context_builder
    state = getattr(session, "agent_state", None)
    try:
        summary = await ctx.compact(focus=focus, restore_state=state)
        await session._send_event({
            "type": "context_compacted",
            "data": {
                "summary": summary,
                "focus": focus,
                "compaction_count": getattr(ctx, "_compaction_count", 0),
                "estimated_tokens": ctx._history_tokens_total,
            },
        })
    except Exception as exc:
        logger.warning("Manual compact failed: %s", exc)
        await session._send_event({
            "type": "error",
            "data": {"message": f"Compact failed: {exc}", "error_type": "context"},
        })
    return True


def _fork_text(value: Any) -> str:
    """Normalize transcript/context content for fork-boundary matching."""
    if isinstance(value, str):
        return " ".join(value.split())
    if isinstance(value, list):
        return " ".join(_fork_text(item) for item in value if item is not None).strip()
    if isinstance(value, dict):
        for key in ("text", "content", "value"):
            if key in value:
                return _fork_text(value.get(key))
    return str(value or "").strip()


def _history_message_matches_transcript(entry: dict[str, Any], history_message: Any) -> bool:
    """Match one persisted transcript item to one model-context message.

    Context history may contain runtime-injected prefixes, so exact equality is
    intentionally not required for user/assistant text. The ordered walk in
    ``_resolve_context_history_index`` prevents duplicate text from selecting
    an earlier turn.
    """
    role = str(entry.get("role") or "").strip().lower()
    history_role = str(getattr(history_message, "role", "") or "").strip().lower()
    if role != history_role or role not in {"user", "assistant"}:
        return False

    transcript_content = _fork_text(entry.get("content"))
    history_content = _fork_text(getattr(history_message, "content", ""))
    if transcript_content and history_content:
        if transcript_content == history_content:
            return True
        # Runtime context and attachment fallbacks are prepended to user turns.
        if role == "user" and transcript_content in history_content:
            return True
        # Providers may normalize assistant whitespace or append structured
        # text around the persisted final answer.
        if role == "assistant" and (
            transcript_content in history_content or history_content in transcript_content
        ):
            return True
        return False

    if transcript_content or history_content:
        return False
    transcript_calls = entry.get("tool_calls")
    history_calls = getattr(history_message, "tool_calls", None)
    return bool(transcript_calls or history_calls or role == "assistant")


def _resolve_context_history_index(
    context_builder: Any,
    transcript: list[dict[str, Any]],
    target_transcript_index: int,
) -> int | None:
    """Resolve a transcript boundary to the corresponding model-history index.

    The two sequences are deliberately not assumed to have the same length:
    model history can contain runtime context, tool messages, or compaction
    boundaries that are not persisted as user-facing transcript messages.
    """
    history = list(getattr(context_builder, "_history", []) or [])
    if target_transcript_index < 0 or target_transcript_index >= len(transcript):
        return None
    cursor = 0
    target_history_index: int | None = None
    forward_complete = True
    for transcript_index, entry in enumerate(transcript[: target_transcript_index + 1]):
        role = str(entry.get("role") or "").strip().lower()
        if role not in {"user", "assistant"}:
            continue
        match_index: int | None = None
        for history_index in range(cursor, len(history)):
            if _history_message_matches_transcript(entry, history[history_index]):
                match_index = history_index
                break
        if match_index is None:
            forward_complete = False
            break
        cursor = match_index + 1
        if transcript_index == target_transcript_index:
            target_history_index = match_index
    if forward_complete and target_history_index is not None:
        return target_history_index

    # Compaction can replace an old transcript prefix with a summary message.
    # Align the target against the retained suffix from the end so a recent
    # message remains forkable without pretending the old prefix still exists.
    cursor = len(history) - 1
    for transcript_index in range(len(transcript) - 1, target_transcript_index - 1, -1):
        entry = transcript[transcript_index]
        role = str(entry.get("role") or "").strip().lower()
        if role not in {"user", "assistant"}:
            continue
        match_index = None
        for history_index in range(cursor, -1, -1):
            if _history_message_matches_transcript(entry, history[history_index]):
                match_index = history_index
                break
        if match_index is None:
            return None
        cursor = match_index - 1
        if transcript_index == target_transcript_index:
            return match_index
    return None


async def handle_context_fork(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    """Fork from a stable transcript message id, with index compatibility."""
    requested_message_id = str(data.get("message_id") or "").strip()
    requested_message_index = int(data.get("message_index", -1))
    source_conversation = getattr(session, "active_conversation", None)
    source_transcript = list(getattr(source_conversation, "transcript", []) or [])
    transcript_index = requested_message_index
    if not requested_message_id and source_transcript and transcript_index < 0:
        # Keep legacy negative-index clients semantically aligned with the
        # persisted transcript before calculating the model-context boundary.
        transcript_index = max(0, len(source_transcript) + transcript_index)
    if requested_message_id:
        transcript_index = next(
            (
                index
                for index, entry in enumerate(source_transcript)
                if str(entry.get("id") or "").strip() == requested_message_id
            ),
            -1,
        )
        if transcript_index < 0:
            await session._send_event({
                "type": "error",
                "data": {
                    "message": f"Fork target message not found: {requested_message_id}",
                    "error_type": "context",
                },
            })
            return True

    ctx = session.context_builder
    try:
        context_history_index = transcript_index
        if requested_message_id:
            resolved = _resolve_context_history_index(ctx, source_transcript, transcript_index)
            if resolved is None:
                await session._send_event({
                    "type": "error",
                    "data": {
                        "message": (
                            "Fork target exists in the transcript but is no longer "
                            "available in the active model context. Compact or restore "
                            "the conversation before forking from it."
                        ),
                        "error_type": "context",
                    },
                })
                return True
            context_history_index = resolved
        forked = ctx.fork_from(context_history_index)
        # Store the forked context for a subsequent side_query or new turn
        if not hasattr(session, "_forked_contexts"):
            session._forked_contexts = {}
        registry = getattr(session, "_fork_registry", None)
        if registry is not None:
            fork_record = registry.create(
                parent_conversation_id=str(session.active_conversation_id or ""),
                message_index=transcript_index,
                history_length=len(forked._history),
                estimated_tokens=forked._history_tokens_total,
            )
            fork_id = fork_record.fork_id
        else:
            # Test-only sessions without a durable session id still receive a
            # stable-in-process id, never Python's object address.
            from uuid import uuid4

            fork_id = f"fork_{uuid4().hex[:16]}"
        session._forked_contexts[fork_id] = forked
        fork_data = {
            "fork_id": fork_id,
            "message_index": transcript_index,
            "context_history_index": context_history_index,
            "history_length": len(forked._history),
            "estimated_tokens": forked._history_tokens_total,
            "parent_conversation_id": str(session.active_conversation_id or ""),
        }
        if requested_message_id:
            fork_data["message_id"] = requested_message_id
        if registry is not None:
            fork_data.update(registry.get(fork_id).to_dict())
        if source_conversation is not None and bool(data.get("create_branch", True)):
            branch_transcript = source_transcript[: max(0, transcript_index + 1)]
            branch_id = fork_id
            branch = session.conversation_repo.create_conversation(
                conversation_id=branch_id,
                title=f"{getattr(source_conversation, 'title', 'Conversation')} · 分支",
                memory_mode=getattr(source_conversation, "memory_mode", "none"),
                permission_mode=getattr(source_conversation, "permission_mode", "default"),
                permission_deny_rules=list(getattr(source_conversation, "permission_deny_rules", []) or []),
                permission_overrides=dict(getattr(source_conversation, "permission_overrides", {}) or {}),
                summary=str(getattr(source_conversation, "summary", "") or ""),
                inherited_facts=list(getattr(source_conversation, "inherited_facts", []) or []),
                local_facts=list(getattr(source_conversation, "local_facts", []) or []),
                transcript=copy.deepcopy(branch_transcript),
                context_snapshot=forked.export_snapshot(),
                workspace_root=str(
                    getattr(source_conversation, "worktree_path", "")
                    or getattr(source_conversation, "workspace_root", "")
                    or ""
                ),
                git_branch=str(getattr(source_conversation, "git_branch", "") or ""),
                worktree_path="",
                # Context branches share the checkout but never own/delete the
                # source conversation's isolated worktree.
                git_isolated=False,
                parent_conversation_id=str(source_conversation.id),
                parent_message_index=transcript_index,
                fork_id=fork_id,
                branch_kind="context_fork",
            )
            if registry is not None:
                bound = registry.bind_branch(fork_id, branch.id)
                if bound is not None:
                    fork_data.update(bound.to_dict())
            fork_data.update({
                "branch_conversation_id": branch.id,
                "branch_created": True,
            })
            if bool(data.get("activate", False)):
                session.active_conversation_id = branch.id
                await session._switch_workspace_for_conversation(branch, announce=False)
                session._load_active_conversation_snapshot(branch.id, branch.context_snapshot)
                session._sync_permission_mode_with_active_conversation(source="context.fork")
            send_conversation_list = getattr(session, "_send_conversation_list", None)
            if callable(send_conversation_list):
                await send_conversation_list()
        await session._send_event({
            "type": "context_forked",
            "data": fork_data,
        })
    except Exception as exc:
        logger.warning("Context fork failed: %s", exc)
        await session._send_event({
            "type": "error",
            "data": {"message": f"Fork failed: {exc}", "error_type": "context"},
        })
    return True


async def handle_context_side_query(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    """Run a transient side query without modifying the main context."""
    query = str(data.get("query") or "").strip()
    focus = str(data.get("focus") or "").strip()
    if not query:
        return True
    ctx = session.context_builder
    try:
        result = await ctx.side_query(query, focus=focus)
        await session._send_event({
            "type": "context_side_query_result",
            "data": {
                "query": query,
                "result": result,
                "focus": focus,
            },
        })
    except Exception as exc:
        logger.warning("Side query failed: %s", exc)
        await session._send_event({
            "type": "error",
            "data": {"message": f"Side query failed: {exc}", "error_type": "context"},
        })
    return True


async def handle_context_ledger(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    """Return the Context Ledger — a structured audit of context composition."""
    ctx = session.context_builder
    try:
        ledger = ctx.context_ledger()
        await session._send_event({
            "type": "context_ledger",
            "data": ledger,
        })
    except Exception as exc:
        logger.warning("Context ledger failed: %s", exc)
        await session._send_event({
            "type": "error",
            "data": {"message": f"Ledger failed: {exc}", "error_type": "context"},
        })
    return True


HANDLERS: dict[str, Any] = {
    "conversation.create": handle_conversation_create,
    "conversation.clone": handle_conversation_clone,
    "conversation.merge": handle_conversation_merge,
    "conversation.export": handle_conversation_export,
    "conversation.switch": handle_conversation_switch,
    "conversation.list": handle_conversation_list,
    "conversation.rename": handle_conversation_rename,
    "conversation.archive": handle_conversation_archive,
    "conversation.unarchive": handle_conversation_unarchive,
    "conversation.delete": handle_conversation_delete,
    "conversation.clear": handle_conversation_clear,
    "conversation.truncate": handle_conversation_truncate,
    "conversation.worktree.cleanup": handle_conversation_worktree_cleanup,
    "conversation.worktree.handoff.preflight": handle_conversation_worktree_handoff_preflight,
    "conversation.worktree.handoff.execute": handle_conversation_worktree_handoff_execute,
    "conversation.memory_mode.set": handle_conversation_memory_mode_set,
    "conversation.permission_mode.set": handle_conversation_permission_mode_set,
    "conversation.goal.set": handle_conversation_goal_set,
    "conversation.permission.rules.list": handle_conversation_permission_rules_list,
    "conversation.permission.rules.add": handle_conversation_permission_rules_add,
    "conversation.permission.rules.remove": handle_conversation_permission_rules_remove,
    "permissions.content_rule.add": handle_permissions_content_rule_add,
    "context.compact": handle_context_compact,
    "context.fork": handle_context_fork,
    "context.side_query": handle_context_side_query,
    "context.ledger": handle_context_ledger,
}
