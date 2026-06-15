from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, TYPE_CHECKING

from backend.agent.message import AgentEvent
from backend.conversations.models import DEFAULT_CONVERSATION_PERMISSION_MODE, utc_now_iso
from backend.ws.conversation_errors import emit_conversation_not_found
from backend.ws.utils import (
    normalize_permission_level,
    normalize_permission_mode,
    normalize_permission_overrides,
    normalize_tool_patterns,
    permission_level_to_token,
    serialize_permission_overrides,
)

if TYPE_CHECKING:
    from backend.ws.handler import WebSocketSession

logger = logging.getLogger(__name__)


async def handle_conversation_create(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    memory_mode = str(data.get("memory_mode", "none"))
    git_isolated = bool(data.get("git_isolated") or data.get("gitIsolated"))
    activate = not bool(data.get("side_chat") or data.get("sideChat"))
    workspace_root = str(data.get("workspace_root") or data.get("workspaceRoot") or "").strip()
    permission_mode = normalize_permission_mode(
        str(data.get("permission_mode") or data.get("permissionMode") or DEFAULT_CONVERSATION_PERMISSION_MODE)
    ) or DEFAULT_CONVERSATION_PERMISSION_MODE
    if git_isolated and not workspace_root:
        await session._send_event(
            AgentEvent.error(
                "Open a workspace folder before creating an isolated Git session.",
                recoverable=True,
                error_type="workspace",
                error_code="workspace_required",
            )
        )
        return True
    base_summary, snapshot, inherited_facts = session._build_inherited_snapshot(memory_mode)
    created = session.conversation_repo.create_conversation(
        conversation_id=str(data.get("conversation_id") or data.get("conversationId") or "").strip() or None,
        title=str(data.get("title") or "New chat"),
        memory_mode=memory_mode,
        permission_mode=permission_mode,
        summary=base_summary,
        inherited_facts=inherited_facts,
        context_snapshot=snapshot,
        workspace_root=workspace_root,
        git_isolated=git_isolated,
    )
    if git_isolated:
        created = await session._create_isolated_conversation_worktree(created) or created
    elif workspace_root:
        created = session.conversation_repo.update_workspace_binding(
            created.id,
            workspace_root=workspace_root,
            git_branch=session._git_branch_for(Path(workspace_root)),
            worktree_path="",
            git_isolated=False,
        ) or created
    if activate:
        session.active_conversation_id = created.id
    if activate:
        if workspace_root:
            await session._switch_workspace_for_conversation(created, announce=False)
        else:
            clear_runtime = getattr(session, "_clear_workspace_runtime", None)
            if callable(clear_runtime):
                clear_runtime()
    if activate:
        session._load_active_conversation_snapshot(created.id, created.context_snapshot)
        session._sync_permission_mode_with_active_conversation(source="conversation.create")
    await session._send_conversation_list()
    return True


async def handle_conversation_switch(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    conversation_id = str(data.get("conversation_id", ""))
    target = session.conversation_repo.get_conversation(conversation_id)
    if target is None:
        await emit_conversation_not_found(session, conversation_id)
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
        {
            "type": "conversation.switched",
            "conversation_id": target.id,
            "conversation": target.to_dict(),
            "is_hydrating": is_hydrating,
            "session": session.runtime_snapshot(),
        },
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
    target = None
    if preferred_id:
        candidate = session.conversation_repo.get_conversation(preferred_id)
        if candidate is not None and not getattr(candidate, "archived", False):
            target = candidate

    if target is None:
        conversations = [
            item
            for item in session.conversation_repo.list_conversations()
            if not getattr(item, "archived", False)
        ]
        if not conversations:
            conversations = session.conversation_repo.list_conversations()
        if conversations:
            target = session.conversation_repo.get_conversation(conversations[0].id)

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
    conversation_id = str(data.get("conversation_id", ""))
    title = str(data.get("title", ""))
    updated = session.conversation_repo.rename_conversation(conversation_id, title)
    if updated is None:
        await emit_conversation_not_found(session, conversation_id)
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
    conversation_id = str(data.get("conversation_id", ""))
    cleanup_worktree = bool(data.get("cleanup_worktree") or data.get("cleanupWorktree"))
    force_cleanup = bool(data.get("force"))
    target = session.conversation_repo.get_conversation(conversation_id)
    if cleanup_worktree and target is not None:
        cleanup = await _cleanup_conversation_worktree(session, target, force=force_cleanup)
        if not cleanup.get("removed") and cleanup.get("needs_force"):
            await session._emit_command_result(
                "conversation.worktree.cleanup",
                str(cleanup.get("error") or "Worktree has local changes; force is required"),
                level="warning",
                data=cleanup,
            )
            return True
    deleted = session.conversation_repo.delete_conversation(conversation_id)
    if not deleted:
        await emit_conversation_not_found(session, conversation_id)
        return True
    if session.active_conversation_id == conversation_id:
        await _activate_conversation_or_blank(session)
    await session._send_conversation_list()
    return True


async def handle_conversation_worktree_cleanup(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    conversation_id = str(data.get("conversation_id", "")).strip()
    target = session.conversation_repo.get_conversation(conversation_id)
    if target is None:
        await emit_conversation_not_found(session, conversation_id)
        return True

    cleanup = await _cleanup_conversation_worktree(session, target, force=bool(data.get("force")))
    level = "success" if cleanup.get("removed") else "warning"
    await session._emit_command_result(
        "conversation.worktree.cleanup",
        str(cleanup.get("message") or cleanup.get("error") or "Worktree cleanup finished"),
        level=level,
        data=cleanup,
    )
    if cleanup.get("removed"):
        updated = session.conversation_repo.update_workspace_binding(
            target.id,
            workspace_root=str(getattr(target, "workspace_root", "") or ""),
            git_branch=str(getattr(target, "git_branch", "") or ""),
            worktree_path="",
            git_isolated=False,
        )
        if updated is not None:
            await session._send_conversation_list()
    return True


async def handle_conversation_clear(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    conversation_id = str(data.get("conversation_id") or session.active_conversation_id or "").strip()
    if not conversation_id:
        return False
    target = session.conversation_repo.get_conversation(conversation_id)
    if target is None:
        return False
    session.conversation_repo.replace_transcript(conversation_id, [])
    session.conversation_repo.update_summary(conversation_id, "")
    session.conversation_repo.update_facts(conversation_id, local_facts=[])
    session.conversation_repo.save_context_snapshot(conversation_id, {})
    session.context_builder.clear()
    session._load_active_conversation_snapshot(conversation_id, {})
    await session._send_conversation_list()
    await session._send_ws_payload(
        {
            "type": "conversation.switched",
            "conversation_id": conversation_id,
            "conversation": session.conversation_repo.get_conversation(conversation_id).to_dict(),
            "is_hydrating": False,
            "session": session.runtime_snapshot(),
        },
        log_context="conversation.switched",
    )
    await session._emit_command_result("clear", "Conversation history cleared.")
    return True


async def handle_conversation_memory_mode_set(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    conversation_id = str(data.get("conversation_id") or session.active_conversation_id or "")
    memory_mode = str(data.get("memory_mode", "none"))
    updated = session.conversation_repo.update_memory_mode(conversation_id, memory_mode)
    if updated is not None:
        await session._send_conversation_list()
    return True


async def handle_conversation_permission_mode_set(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    requested = normalize_permission_mode(str(data.get("mode") or data.get("permission_mode") or ""))
    if requested is None:
        await session._send_event(
            AgentEvent.error(
                "Invalid permission mode. Use confirm|auto|bypass. Legacy modes still accepted: default|plan|accept_edits",
                recoverable=True,
                error_type="tool",
            )
        )
        return True

    explicit_conversation_id = str(data.get("conversation_id") or "").strip()
    source = str(data.get("source") or "websocket.command").strip() or "websocket.command"
    if not explicit_conversation_id and not session.active_conversation_id:
        if session._set_permission_context_mode(requested, source=source):
            await session._emit_permission_mode_updated()
            await session._send_task_runtime_update()
        await session._send_conversation_list()
        return True
    conversation_id = str(explicit_conversation_id or session.active_conversation_id or "").strip()
    if not conversation_id:
        await session._send_event(
            AgentEvent.error("No active conversation to update", recoverable=True, error_type="tool")
        )
        return True

    updated = session.conversation_repo.update_permission_mode(conversation_id, requested)
    if updated is None:
        await emit_conversation_not_found(session, conversation_id)
        return True

    if conversation_id == session.active_conversation_id:
        session._set_permission_context_mode(requested, source=source)
        await session._emit_permission_mode_updated()
        await session._send_task_runtime_update()

    await session._send_conversation_list()
    return True


def _resolve_goal_target(session: "WebSocketSession", data: dict[str, Any]) -> tuple[str, Any | None]:
    explicit_conversation_id = str(data.get("conversation_id") or "").strip()
    conversation_id = str(explicit_conversation_id or session.active_conversation_id or "").strip()
    if not conversation_id:
        return "", None
    return conversation_id, session.conversation_repo.get_conversation(conversation_id)


async def _emit_goal_updated(
    session: "WebSocketSession",
    *,
    conversation_id: str,
    goal: dict[str, Any],
    source: str,
) -> None:
    await session._send_ws_payload(
        {
            "type": "goal.updated",
            "conversation_id": conversation_id,
            "goal": dict(goal or {}),
            "source": source,
        },
        log_context="goal.updated",
    )


async def handle_conversation_goal_set(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    conversation_id, target = _resolve_goal_target(session, data)
    source = str(data.get("source") or "websocket.command").strip() or "websocket.command"
    if not conversation_id:
        await session._emit_command_result("goal", "No active conversation to update.", level="warning")
        return True
    if target is None:
        await session._emit_command_result("goal", f"Conversation '{conversation_id}' not found.", level="error")
        return True

    action = str(data.get("action") or "set").strip().lower()
    current_goal = dict(getattr(target, "goal", {}) or {})

    if action in {"show", "status", "inspect"}:
        await _emit_goal_updated(
            session,
            conversation_id=conversation_id,
            goal=current_goal,
            source=source,
        )
        if current_goal.get("text"):
            await session._emit_command_result(
                "goal",
                f"Goal is {current_goal.get('status', 'active')}: {current_goal.get('text')}",
                data={"conversation_id": conversation_id, "goal": current_goal},
            )
        else:
            await session._emit_command_result(
                "goal",
                "No goal is set for this conversation.",
                data={"conversation_id": conversation_id, "goal": {}},
            )
        return True

    if action in {"clear", "delete", "reset"}:
        updated = session.conversation_repo.update_goal(conversation_id, {})
        if updated is None:
            await session._emit_command_result("goal", f"Conversation '{conversation_id}' not found.", level="error")
            return True
        if conversation_id == session.active_conversation_id:
            await _emit_goal_updated(session, conversation_id=conversation_id, goal={}, source=source)
        await session._send_conversation_list()
        await session._emit_command_result(
            "goal",
            "Cleared the conversation goal.",
            data={"conversation_id": conversation_id, "goal": {}},
        )
        return True

    if action in {"pause", "resume"}:
        if not current_goal.get("text"):
            await session._emit_command_result(
                "goal",
                "No goal is set for this conversation.",
                level="warning",
                data={"conversation_id": conversation_id, "goal": {}},
            )
            return True
        next_goal = {
            **current_goal,
            "status": "paused" if action == "pause" else "active",
            "updated_at": utc_now_iso(),
            "source": source,
        }
        updated = session.conversation_repo.update_goal(conversation_id, next_goal)
        if updated is None:
            await session._emit_command_result("goal", f"Conversation '{conversation_id}' not found.", level="error")
            return True
        if conversation_id == session.active_conversation_id:
            await _emit_goal_updated(session, conversation_id=conversation_id, goal=next_goal, source=source)
        await session._send_conversation_list()
        await session._emit_command_result(
            "goal",
            f"Goal {next_goal['status']}.",
            data={"conversation_id": conversation_id, "goal": next_goal},
        )
        return True

    text = str(data.get("text") or data.get("goal") or "").strip()
    if not text:
        await session._emit_command_result(
            "goal",
            "Usage: /goal <target> | /goal pause | /goal resume | /goal clear",
            level="warning",
        )
        return True

    now = utc_now_iso()
    next_goal = {
        "id": str(current_goal.get("id") or f"goal_{conversation_id}"),
        "text": text[:4000],
        "status": "active",
        "created_at": str(current_goal.get("created_at") or now),
        "updated_at": now,
        "source": source,
    }
    updated = session.conversation_repo.update_goal(conversation_id, next_goal)
    if updated is None:
        await session._emit_command_result("goal", f"Conversation '{conversation_id}' not found.", level="error")
        return True
    if conversation_id == session.active_conversation_id:
        await _emit_goal_updated(session, conversation_id=conversation_id, goal=next_goal, source=source)
    await session._send_conversation_list()
    await session._emit_command_result(
        "goal",
        "Updated the conversation goal.",
        data={"conversation_id": conversation_id, "goal": next_goal},
    )
    return True


def _resolve_permission_rule_target(session: "WebSocketSession", data: dict[str, Any]) -> tuple[str, Any | None]:
    explicit_conversation_id = str(data.get("conversation_id") or "").strip()
    conversation_id = str(explicit_conversation_id or session.active_conversation_id or "").strip()
    if not conversation_id:
        return "", None
    return conversation_id, session.conversation_repo.get_conversation(conversation_id)


async def handle_conversation_permission_rules_list(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    conversation_id, target = _resolve_permission_rule_target(session, data)
    if not conversation_id:
        await session._emit_command_result("permissions.rules.list", "No active conversation to inspect", level="warning")
        return True
    if target is None:
        await session._emit_command_result("permissions.rules.list", f"Conversation '{conversation_id}' not found", level="error")
        return True

    source = str(data.get("source") or "websocket.command").strip() or "websocket.command"
    await session._emit_permission_rules_updated(conversation_id=conversation_id, source=source)
    rules = session._build_permission_rules_payload(conversation=target)
    message = (
        f"Permission rules: mode {rules['mode']} | "
        f"session deny {len(rules['session_deny'])} | "
        f"overrides {len(rules['session_overrides'])} | "
        f"system deny {len(rules['system_deny'])}"
    )
    await session._emit_command_result(
        "permissions.rules.list", message,
        data={"conversation_id": conversation_id, "rules": rules},
    )
    return True


async def handle_conversation_permission_rules_add(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    conversation_id, target = _resolve_permission_rule_target(session, data)
    if not conversation_id:
        await session._emit_command_result("permissions.rules.add", "No active conversation to update", level="warning")
        return True
    if target is None:
        await session._emit_command_result("permissions.rules.add", f"Conversation '{conversation_id}' not found", level="error")
        return True

    rule_kind = str(data.get("rule_kind") or data.get("kind") or "deny").strip().lower()
    pattern = str(data.get("pattern") or "").strip()
    if not pattern:
        await session._emit_command_result(
            "permissions.rules.add",
            "Pattern is required. Use /permissions rules add deny <pattern> or /permissions rules add override <pattern> <level>",
            level="warning",
        )
        return True

    deny_rules = normalize_tool_patterns(getattr(target, "permission_deny_rules", []))
    overrides = normalize_permission_overrides(getattr(target, "permission_overrides", {}))
    level = None
    result_level = "success"

    if rule_kind in {"deny", "block"}:
        already_present = pattern in deny_rules
        if not already_present:
            deny_rules.append(pattern)
        result_message = f"Added deny rule: {pattern}"
        if already_present:
            result_level = "info"
            result_message = f"Deny rule already present: {pattern}"
    elif rule_kind in {"override", "level"}:
        level = normalize_permission_level(data.get("level"))
        if level is None:
            await session._emit_command_result("permissions.rules.add", "Invalid level. Use auto|confirm|diff|deny", level="warning")
            return True
        previous_level = overrides.get(pattern)
        overrides[pattern] = level
        result_message = f"Added override rule: {pattern} -> {permission_level_to_token(level)}"
        if previous_level == level:
            result_level = "info"
            result_message = f"Override rule already present: {pattern} -> {permission_level_to_token(level)}"
    else:
        await session._emit_command_result("permissions.rules.add", "Invalid rule kind. Use deny or override", level="warning")
        return True

    updated = session.conversation_repo.update_permission_rules(
        conversation_id, deny_rules=deny_rules, overrides=serialize_permission_overrides(overrides),
    )
    if updated is None:
        await session._emit_command_result("permissions.rules.add", f"Conversation '{conversation_id}' not found", level="error")
        return True

    source = str(data.get("source") or "websocket.command").strip() or "websocket.command"
    if conversation_id == session.active_conversation_id:
        session._set_permission_context_rules(session_overrides=overrides, tool_deny_rules=deny_rules, source=source)
        await session._send_task_runtime_update()

    await session._emit_permission_rules_updated(conversation_id=conversation_id, source=source)
    payload: dict[str, Any] = {"conversation_id": conversation_id, "rule_kind": "override" if rule_kind in {"override", "level"} else "deny", "pattern": pattern}
    if level is not None:
        payload["level"] = permission_level_to_token(level)
    await session._emit_command_result("permissions.rules.add", result_message, level=result_level, data=payload)
    return True


async def handle_conversation_permission_rules_remove(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    conversation_id, target = _resolve_permission_rule_target(session, data)
    if not conversation_id:
        await session._emit_command_result("permissions.rules.remove", "No active conversation to update", level="warning")
        return True
    if target is None:
        await session._emit_command_result("permissions.rules.remove", f"Conversation '{conversation_id}' not found", level="error")
        return True

    rule_kind = str(data.get("rule_kind") or data.get("kind") or "deny").strip().lower()
    pattern = str(data.get("pattern") or "").strip()
    if not pattern:
        await session._emit_command_result(
            "permissions.rules.remove",
            "Pattern is required. Use /permissions rules remove deny <pattern> or /permissions rules remove override <pattern>",
            level="warning",
        )
        return True

    deny_rules = normalize_tool_patterns(getattr(target, "permission_deny_rules", []))
    overrides = normalize_permission_overrides(getattr(target, "permission_overrides", {}))
    removed = False

    if rule_kind in {"deny", "block"}:
        removed = pattern in deny_rules
        deny_rules = [item for item in deny_rules if item != pattern]
        result_message = f"Removed deny rule: {pattern}"
        result_level = "success"
        if not removed:
            result_message = f"No deny rule matched: {pattern}"
            result_level = "info"
    elif rule_kind in {"override", "level"}:
        removed = pattern in overrides
        overrides.pop(pattern, None)
        result_message = f"Removed override rule: {pattern}"
        result_level = "success"
        if not removed:
            result_message = f"No override rule matched: {pattern}"
            result_level = "info"
    else:
        await session._emit_command_result("permissions.rules.remove", "Invalid rule kind. Use deny or override", level="warning")
        return True

    updated = session.conversation_repo.update_permission_rules(
        conversation_id, deny_rules=deny_rules, overrides=serialize_permission_overrides(overrides),
    )
    if updated is None:
        await session._emit_command_result("permissions.rules.remove", f"Conversation '{conversation_id}' not found", level="error")
        return True

    source = str(data.get("source") or "websocket.command").strip() or "websocket.command"
    if conversation_id == session.active_conversation_id:
        session._set_permission_context_rules(session_overrides=overrides, tool_deny_rules=deny_rules, source=source)
        await session._send_task_runtime_update()

    await session._emit_permission_rules_updated(conversation_id=conversation_id, source=source)
    await session._emit_command_result(
        "permissions.rules.remove", result_message, level=result_level,
        data={"conversation_id": conversation_id, "rule_kind": "override" if rule_kind in {"override", "level"} else "deny", "pattern": pattern},
    )
    return True


async def _cleanup_conversation_worktree(session: "WebSocketSession", conversation: Any, *, force: bool = False) -> dict[str, Any]:
    from backend.workspace.worktree import WorktreeManager

    worktree_path = Path(str(getattr(conversation, "worktree_path", "") or "")).resolve()
    if not str(worktree_path) or not getattr(conversation, "git_isolated", False):
        return {
            "removed": False, "conversation_id": getattr(conversation, "id", ""),
            "path": str(worktree_path) if str(worktree_path) != "." else "",
            "error": "Conversation is not bound to an isolated worktree",
        }
    if not worktree_path.exists():
        return {"removed": True, "conversation_id": conversation.id, "path": str(worktree_path), "message": "Isolated worktree already removed"}
    if worktree_path == session._current_workspace_root():
        return {"removed": False, "conversation_id": conversation.id, "path": str(worktree_path), "error": "Cannot remove the active workspace worktree"}

    base_root = session._main_worktree_root(worktree_path)
    isolated_root = (base_root / ".claude" / "worktrees").resolve()
    if not session._is_path_within(worktree_path, isolated_root):
        return {"removed": False, "conversation_id": conversation.id, "path": str(worktree_path), "error": "Only isolated worktrees under .claude/worktrees can be removed"}

    dirty = session._worktree_has_local_changes(worktree_path)
    if dirty and not force:
        return {"removed": False, "conversation_id": conversation.id, "path": str(worktree_path), "needs_force": True, "error": "Worktree has local changes; confirm force cleanup to remove it"}

    try:
        manager = WorktreeManager(base_root)
        removal = manager.safe_remove_worktree(
            worktree_path,
            force=force,
            conversation_id=conversation.id,
            branch=str(getattr(conversation, "git_branch", "") or ""),
        )
    except Exception as exc:
        return {"removed": False, "conversation_id": conversation.id, "path": str(worktree_path), "error": str(exc)}

    if removal.needs_force:
        return {"removed": False, "conversation_id": conversation.id, "path": str(worktree_path), "needs_force": True, "error": removal.error}

    result: dict[str, Any] = {
        "removed": removal.removed,
        "conversation_id": conversation.id,
        "path": str(worktree_path),
        "branch": getattr(conversation, "git_branch", ""),
        "message": "Removed isolated worktree" if removal.removed else (removal.error or "git worktree remove failed"),
    }
    if not removal.removed and removal.error:
        result["error"] = removal.error
    if removal.snapshot is not None:
        result["snapshot_id"] = removal.snapshot.id
        result["snapshot_ref"] = removal.snapshot.snapshot_ref
        if removal.removed:
            result["message"] = "Removed isolated worktree (snapshot saved; recoverable)"
    return result


HANDLERS: dict[str, Any] = {
    "conversation.create": handle_conversation_create,
    "conversation.switch": handle_conversation_switch,
    "conversation.list": handle_conversation_list,
    "conversation.rename": handle_conversation_rename,
    "conversation.archive": handle_conversation_archive,
    "conversation.unarchive": handle_conversation_unarchive,
    "conversation.delete": handle_conversation_delete,
    "conversation.clear": handle_conversation_clear,
    "conversation.worktree.cleanup": handle_conversation_worktree_cleanup,
    "conversation.memory_mode.set": handle_conversation_memory_mode_set,
    "conversation.permission_mode.set": handle_conversation_permission_mode_set,
    "conversation.goal.set": handle_conversation_goal_set,
    "conversation.permission.rules.list": handle_conversation_permission_rules_list,
    "conversation.permission.rules.add": handle_conversation_permission_rules_add,
    "conversation.permission.rules.remove": handle_conversation_permission_rules_remove,
}
