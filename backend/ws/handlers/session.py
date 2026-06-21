from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

from backend.agent.message import AgentEvent

if TYPE_CHECKING:
    from backend.ws.handler import WebSocketSession

logger = logging.getLogger(__name__)

RUNTIME_PROTOCOL_VERSION = "1.0.0"


async def handle_session_tasks_inspect(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    snapshot = session.runtime_snapshot()
    running_tasks = snapshot.get("running_tasks", [])
    summary = snapshot.get("task_summary", {})
    if running_tasks:
        preview = " | ".join(
            f"{task.get('kind', 'task')} ({task.get('status', 'unknown')})"
            for task in running_tasks[:3]
        )
        message = f"Current session tasks: {preview}"
    else:
        message = "Current session tasks: no running tasks"
    await session._emit_command_result(
        "tasks",
        message,
        data={
            "session_id": session.session_id,
            "task_summary": summary,
            "running_tasks": running_tasks,
        },
    )
    return True


async def handle_session_status_inspect(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.main import get_mcp_status

    mcp_status = get_mcp_status()
    connected_mcp = [
        server for server in mcp_status if str(server.get("status", "")).strip().lower() == "connected"
    ]
    active_skills = sorted(session.skill_manager.get_active_names()) if session.skill_manager is not None else []
    snapshot = session.runtime_snapshot()
    message = (
        f"Runtime status: model {session.selected_model or 'unknown'} | "
        f"mode {session.permission_context.mode} | "
        f"MCP connected {len(connected_mcp)}/{len(mcp_status)} | "
        f"active skills {len(active_skills)} | "
        f"running tasks {snapshot.get('task_summary', {}).get('running', 0)}"
    )
    await session._emit_command_result(
        "status",
        message,
        data={
            "session_id": session.session_id,
            "selected_model": session.selected_model,
            "permission_mode": session.permission_context.mode,
            "mcp": mcp_status,
            "active_skills": active_skills,
            "runtime": snapshot,
        },
    )
    return True


async def handle_session_usage_inspect(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.llm.cost_tracker import CostTracker

    tracker_summary = CostTracker.get_instance().get_summary()
    state = getattr(session, "_last_agent_state", None)
    if state is None:
        from backend.agent.state import AgentState
        state = AgentState(user_message="")

    # Surface MCP tools connected since this session started before snapshotting.
    session.refresh_tool_registry_if_mcp_changed()

    tool_schemas = None
    try:
        tool_schemas = session.tool_registry.get_schemas(
            budget=getattr(session.config.token_budget, "tool_schemas", 6000),
            permission_checker=session.permission_checker,
            permission_context=session.permission_context,
        )
    except Exception as exc:
        logger.debug("usage tool schema snapshot failed: %s", exc)

    try:
        budget_snapshot = session.context_builder.get_budget_snapshot(
            state=state,
            tool_schemas=tool_schemas,
        )
    except Exception as exc:
        logger.debug("usage budget snapshot failed: %s", exc)
        used = int(getattr(session.context_builder, "token_usage", 0) or 0)
        total = int(getattr(getattr(session.context_builder, "_budget", None), "total", 0) or 0)
        budget_snapshot = {"used": used, "total": total, "breakdown": {}}

    used = int(budget_snapshot.get("used") or 0)
    total = int(budget_snapshot.get("total") or 0)
    percent = round((used / total) * 100, 1) if total > 0 else 0.0
    cost = float(tracker_summary.get("total_cost_usd") or 0.0)
    input_tokens = int(tracker_summary.get("input_tokens") or 0)
    output_tokens = int(tracker_summary.get("output_tokens") or 0)
    message = (
        f"Usage: context {used}/{total} tokens ({percent}%) | "
        f"API tokens in {input_tokens} out {output_tokens} | "
        f"estimated cost ${cost:.4f}"
    )
    conversation_id = str(session.active_conversation_id or "").strip()
    scoped_budget_snapshot = dict(budget_snapshot)
    if conversation_id:
        scoped_budget_snapshot["conversation_id"] = conversation_id
    await session._send_event(AgentEvent(type="budget_update", data=scoped_budget_snapshot))
    context_usage = {"used": used, "limit": total}
    if conversation_id:
        context_usage["conversation_id"] = conversation_id
    await session._send_event(AgentEvent(type="context_usage", data=context_usage))
    # `silent` callers (the usage ring's per-turn auto-refresh) only want the
    # context_usage / budget_update events above to refresh the indicator —
    # they must not append a visible "/usage" notice to the transcript.
    if not data.get("silent"):
        await session._emit_command_result(
            "usage",
            message,
            data={
                "session_id": session.session_id,
                "conversation_id": conversation_id or None,
                "cost": tracker_summary,
                "budget": budget_snapshot,
            },
        )
    return True


async def handle_session_permissions_inspect(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    rules = session._build_permission_rules_payload(conversation=session.active_conversation)
    message = (
        f"Permission mode: {rules['mode']} | "
        f"session deny {len(rules['session_deny'])} | "
        f"overrides {len(rules['session_overrides'])} | "
        f"system deny {len(rules['system_deny'])}"
    )
    await session._emit_command_result(
        "permissions",
        message,
        data={
            "session_id": session.session_id,
            "conversation_id": session.active_conversation_id,
            "rules": rules,
        },
    )
    return True


async def handle_runtime_capabilities_inspect(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    await session._send_runtime_capabilities(source=str(data.get("source") or "runtime.inspect"))
    return True


async def handle_session_restore(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.ws.session_restore import SessionRestoreManager

    last_conversation_id = data.get("last_conversation_id")
    last_workspace_root = data.get("last_workspace_root")

    restore_manager = SessionRestoreManager(session.conversation_repo)
    result = await restore_manager.restore_session(
        session_id=session.session_id,
        last_conversation_id=last_conversation_id,
        last_workspace_root=last_workspace_root,
    )
    restored_conversation = result.get("conversation") if isinstance(result.get("conversation"), dict) else None
    restored_conversation_id = restored_conversation.get("id") if restored_conversation else None
    restored_workspace = result.get("workspace") if isinstance(result.get("workspace"), dict) else None
    active_payload = restored_conversation
    is_hydrating = False
    if restored_conversation_id:
        target = session.conversation_repo.get_conversation(str(restored_conversation_id))
        if target is not None and not getattr(target, "archived", False):
            session.active_conversation_id = target.id
            await session._switch_workspace_for_conversation(target, announce=False)
            is_hydrating = session._load_active_conversation_snapshot(target.id, target.context_snapshot)
            session._sync_permission_mode_with_active_conversation(source="session.restore")
            active_payload = target.to_dict()
        else:
            restored_conversation_id = None
            active_payload = None

    runtime_snapshot = session.runtime_snapshot()
    if restored_conversation_id:
        runtime_snapshot = {
            **runtime_snapshot,
            "active_conversation_id": restored_conversation_id,
            "active_conversation": active_payload,
        }
        restored_permission_mode = str((active_payload or {}).get("permission_mode") or "").strip()
        if restored_permission_mode:
            runtime_snapshot["permission_mode"] = restored_permission_mode
    if restored_workspace:
        runtime_snapshot = {
            **runtime_snapshot,
            "workspace_root": restored_workspace.get("root_path"),
        }

    await session._send_ws_payload(
        {
            "type": "session.restored",
            "session_id": result["session_id"],
            "restored": result["restored"],
            "active_conversation_id": restored_conversation_id,
            "conversation_switched_follows": bool(restored_conversation_id and active_payload),
            "conversation": active_payload,
            "active_conversation": active_payload,
            "workspace": restored_workspace,
            "working_directory": (
                restored_workspace.get("root_path")
                if restored_workspace
                else ""
            ),
            "model": session.selected_model,
            "current_model": session.selected_model,
            "provider": session.provider,
            "available_models": session.available_models,
            "session": runtime_snapshot,
            "messages": result.get("messages", []),
            "error": result.get("error"),
        },
        log_context="session.restored",
    )

    if restored_conversation_id and active_payload:
        await session._send_ws_payload(
            {
                "type": "conversation.switched",
                "conversation_id": restored_conversation_id,
                "conversation": active_payload,
                "is_hydrating": is_hydrating,
                "session": runtime_snapshot,
            },
            log_context="conversation.switched",
        )

    await session._reemit_pending_state()
    return True


async def handle_session_sync(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.ws.session_restore import SessionRestoreManager

    client_version = data.get("client_version", 0)

    restore_manager = SessionRestoreManager(session.conversation_repo)
    result = await restore_manager.sync_session(
        session_id=session.session_id,
        client_version=client_version,
        session_snapshot=session.runtime_snapshot(),
    )
    workspace_root = session._workspace_root_for_conversation()

    await session._send_ws_payload(
        {
            "type": "session.synced",
            "protocol_version": RUNTIME_PROTOCOL_VERSION,
            "session_id": result["session_id"],
            "synced": result["synced"],
            "incremental": result["incremental"],
            "changes": result.get("changes", []),
            "session": result["session"],
            "active_conversation_id": session.active_conversation_id
            if session.active_conversation is not None
            and not getattr(session.active_conversation, "archived", False)
            else None,
            "active_conversation": session.active_conversation.to_dict()
            if session.active_conversation is not None
            and not getattr(session.active_conversation, "archived", False)
            else None,
            "working_directory": str(workspace_root) if workspace_root is not None else "",
            "model": session.selected_model,
            "current_model": session.selected_model,
            "provider": session.provider,
            "available_models": session.available_models,
        },
        log_context="session.synced",
    )
    return True


HANDLERS: dict[str, Any] = {
    "session.tasks.inspect": handle_session_tasks_inspect,
    "session.status.inspect": handle_session_status_inspect,
    "session.usage.inspect": handle_session_usage_inspect,
    "session.permissions.inspect": handle_session_permissions_inspect,
    "runtime.capabilities.inspect": handle_runtime_capabilities_inspect,
    "session.restore": handle_session_restore,
    "session.sync": handle_session_sync,
}
