from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from pathlib import Path
from typing import Any, TYPE_CHECKING

from backend.ws.conversation_errors import emit_conversation_not_found
if TYPE_CHECKING:
    from backend.ws.handler import WebSocketSession

logger = logging.getLogger(__name__)


async def _stop_conversation_run(session: "WebSocketSession", conversation_id: str, *, reason: str) -> None:
    """Finish cancellation before deleting or clearing persisted state."""
    session._run_manager.clear_user_message_queue(conversation_id)
    task = session._running_agent_task_for(conversation_id)
    if task is None:
        return
    await session._cancel_agent_runs(conversation_id=conversation_id, reason=reason)
    with suppress(asyncio.CancelledError, Exception):
        await task


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
        build_worktree_cleanup_force_required_outcome,
        parse_conversation_delete_request,
    )

    request = parse_conversation_delete_request(data)
    target = session.conversation_repo.get_conversation(request.conversation_id)
    if request.cleanup_worktree and target is not None:
        cleanup = await _cleanup_conversation_worktree(session, target, force=request.force_cleanup)
        if not cleanup.get("removed") and cleanup.get("needs_force"):
            outcome = build_worktree_cleanup_force_required_outcome(cleanup)
            await session._emit_command_result(
                outcome.command,
                outcome.message,
                level=outcome.level,
                data=outcome.data,
            )
            return True
    if target is None:
        await emit_conversation_not_found(session, request.conversation_id)
        return True
    await _stop_conversation_run(session, request.conversation_id, reason="conversation_deleted")
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
    preflight = build_handoff_preflight(
        target,
        target=str(data.get("target") or ("local" if getattr(target, "git_isolated", False) else "worktree")),
        conversation_repo=session.conversation_repo,
        main_worktree_root=session._main_worktree_root,
        has_running_turn=session._running_agent_task_for(conversation_id) is not None,
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
    from backend.services.conversation_worktree_handoff_service import build_handoff_preflight, switch_main_checkout
    from backend.workspace.worktree import WorktreeManager

    conversation_id = str(data.get("conversation_id") or "").strip()
    conversation = session.conversation_repo.get_conversation(conversation_id)
    if conversation is None:
        await emit_conversation_not_found(session, conversation_id)
        return True
    target_kind = str(data.get("target") or ("local" if getattr(conversation, "git_isolated", False) else "worktree"))
    preflight = build_handoff_preflight(
        conversation,
        target=target_kind,
        conversation_repo=session.conversation_repo,
        main_worktree_root=session._main_worktree_root,
        has_running_turn=session._running_agent_task_for(conversation_id) is not None,
    )
    if not preflight["allowed"] or str(data.get("fingerprint") or "") != preflight["fingerprint"]:
        await session._emit_command_result(
            "conversation.worktree.handoff.execute",
            "Workspace changed after preflight; review the checks and try again.",
            level="warning",
            data={**preflight, "stale": True},
        )
        return True

    if target_kind == "worktree":
        creation = create_isolated_worktree_binding(
            conversation,
            current_workspace_root=session._current_workspace_root(),
            main_worktree_root=session._main_worktree_root,
        )
        if not creation.created:
            await session._emit_command_result("conversation.worktree.handoff.execute", "Failed to create protected workspace.", level="error", data=preflight)
            return True
        updated = session.conversation_repo.update_workspace_binding(
            conversation_id,
            workspace_root=creation.workspace_root,
            git_branch=creation.git_branch,
            worktree_path=creation.worktree_path,
            git_isolated=True,
        )
    else:
        source_path = Path(str(getattr(conversation, "worktree_path", "") or "")).resolve()
        base_root = session._main_worktree_root(source_path)
        branch = str(getattr(conversation, "git_branch", "") or "").strip()
        if session.active_conversation_id == conversation_id:
            clear_runtime = getattr(session, "_clear_workspace_runtime", None)
            if callable(clear_runtime):
                clear_runtime()
        manager = WorktreeManager(base_root)
        if not manager.remove_worktree(source_path, force=False):
            await session._emit_command_result("conversation.worktree.handoff.execute", "Failed to remove the protected workspace.", level="error", data=preflight)
            return True
        switched, error = switch_main_checkout(base_root, branch)
        if not switched:
            manager.create_worktree(source_path, branch=branch, new_branch=False)
            await session._emit_command_result("conversation.worktree.handoff.execute", f"Failed to switch the local checkout: {error}", level="error", data=preflight)
            return True
        updated = session.conversation_repo.update_workspace_binding(
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
    await _stop_conversation_run(session, request.conversation_id, reason="conversation_cleared")
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


async def handle_context_fork(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    """Fork the conversation context from a specific message index."""
    message_index = int(data.get("message_index", -1))
    ctx = session.context_builder
    try:
        forked = ctx.fork_from(message_index)
        # Store the forked context for a subsequent side_query or new turn
        if not hasattr(session, "_forked_contexts"):
            session._forked_contexts = {}
        fork_id = f"fork_{message_index}_{id(forked)}"
        session._forked_contexts[fork_id] = forked
        await session._send_event({
            "type": "context_forked",
            "data": {
                "fork_id": fork_id,
                "message_index": message_index,
                "history_length": len(forked._history),
                "estimated_tokens": forked._history_tokens_total,
            },
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
