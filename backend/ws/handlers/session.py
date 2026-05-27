from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

from backend.agent.message import AgentEvent
from backend.config import get_available_models

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
    await session._send_event(AgentEvent(type="budget_update", data=budget_snapshot))
    await session._send_event(AgentEvent(type="context_usage", data={"used": used, "limit": total}))
    await session._emit_command_result(
        "usage",
        message,
        data={
            "session_id": session.session_id,
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


async def handle_session_restore(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.ws.session_restore import SessionRestoreManager
    from backend.ws.handlers.conversation import handle_conversation_switch
    from backend.ws.handlers.workspace import handle_workspace_import

    last_conversation_id = data.get("last_conversation_id")
    last_workspace_root = data.get("last_workspace_root")

    restore_manager = SessionRestoreManager(session.conversation_repo)
    result = await restore_manager.restore_session(
        session_id=session.session_id,
        last_conversation_id=last_conversation_id,
        last_workspace_root=last_workspace_root,
    )

    await session._send_ws_payload(
        {
            "type": "session.restored",
            "session_id": result["session_id"],
            "restored": result["restored"],
            "conversation": result.get("conversation"),
            "workspace": result.get("workspace"),
            "messages": result.get("messages", []),
            "error": result.get("error"),
        },
        log_context="session.restored",
    )

    if result.get("conversation") and last_conversation_id:
        await handle_conversation_switch(session, {"conversation_id": last_conversation_id})

    if result.get("workspace") and last_workspace_root:
        await handle_workspace_import(session, {"path": last_workspace_root})

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

    await session._send_ws_payload(
        {
            "type": "session.synced",
            "protocol_version": RUNTIME_PROTOCOL_VERSION,
            "session_id": result["session_id"],
            "synced": result["synced"],
            "incremental": result["incremental"],
            "changes": result.get("changes", []),
            "session": result["session"],
        },
        log_context="session.synced",
    )
    return True


HANDLERS: dict[str, Any] = {
    "session.tasks.inspect": handle_session_tasks_inspect,
    "session.status.inspect": handle_session_status_inspect,
    "session.usage.inspect": handle_session_usage_inspect,
    "session.permissions.inspect": handle_session_permissions_inspect,
    "session.restore": handle_session_restore,
    "session.sync": handle_session_sync,
}
