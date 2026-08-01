from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

from backend.agent.message import AgentEvent
from backend.services.mcp_service import (
    MCPServiceError,
    add_mcp_server,
    get_mcp_status,
    install_marketplace_connector,
    list_marketplace_connectors,
    login_mcp_server,
    remove_mcp_server,
    restart_mcp_server,
)
from backend.ws.command_results import emit_command_error

if TYPE_CHECKING:
    from backend.ws.handler import WebSocketSession

logger = logging.getLogger(__name__)


def _tool_registry_availability_notice(session: "WebSocketSession", refreshed: bool) -> str:
    """Explain the fixed-tool-schema boundary for a busy Agent turn."""

    if refreshed:
        return ""
    has_active_run = getattr(session, "_has_active_run", None)
    if callable(has_active_run) and has_active_run():
        return (
            "Connector configuration was saved. The current Agent turn keeps its "
            "existing tool schema; the connector will be available on the next turn."
        )
    return ""


async def handle_mcp_list(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.api.routes_health import get_mcp_manager

    try:
        servers = get_mcp_status(get_mcp_manager())
    except MCPServiceError as exc:
        await emit_command_error(session, "mcp.list", exc)
        return True
    await session._send_ws_payload(
        {"type": "mcp_status", "servers": servers},
        log_context="mcp_status",
    )
    await session._send_ws_payload(
        {
            "type": "connectors.marketplace.list",
            "connectors": list_marketplace_connectors(get_mcp_manager()),
        },
        log_context="connectors.marketplace.list",
    )
    return True


async def handle_mcp_add(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.api.routes_health import get_mcp_manager

    try:
        servers = await add_mcp_server(get_mcp_manager(), data)
        refreshed = session.refresh_tool_registry_if_mcp_changed(allow_when_busy=False)
    except Exception as exc:
        await emit_command_error(session, "mcp.add", f"Failed to add MCP server: {exc}")
        return True
    await session._send_ws_payload(
        {"type": "mcp_status", "servers": servers},
        log_context="mcp_status",
    )
    await session._send_ws_payload(
        {
            "type": "connectors.marketplace.list",
            "connectors": list_marketplace_connectors(get_mcp_manager()),
        },
        log_context="connectors.marketplace.list",
    )
    notice = _tool_registry_availability_notice(session, refreshed)
    if notice:
        await session._send_event(AgentEvent(type="system_notice", data={"content": notice}))
    return True


async def handle_mcp_remove(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.api.routes_health import get_mcp_manager

    try:
        servers = await remove_mcp_server(get_mcp_manager(), data.get("name", ""))
        session.refresh_tool_registry_if_mcp_changed(allow_when_busy=False)
    except MCPServiceError as exc:
        await emit_command_error(session, "mcp.remove", exc)
        return True
    await session._send_ws_payload(
        {"type": "mcp_status", "servers": servers},
        log_context="mcp_status",
    )
    return True


async def handle_mcp_restart(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.api.routes_health import get_mcp_manager

    try:
        servers = await restart_mcp_server(get_mcp_manager(), data.get("name", ""))
        session.refresh_tool_registry_if_mcp_changed(allow_when_busy=False)
    except MCPServiceError as exc:
        await emit_command_error(session, "mcp.restart", exc)
        return True
    await session._send_ws_payload(
        {"type": "mcp_status", "servers": servers},
        log_context="mcp_status",
    )
    return True


# ---------------------------------------------------------------------------
# Env vault handlers
# ---------------------------------------------------------------------------


async def handle_env_list(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.env_vault_service import list_env_entries

    await session._send_ws_payload(
        {"type": "env.list", "entries": list_env_entries().entries},
        log_context="env.list",
    )
    return True


async def handle_env_set(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.env_vault_service import EnvVaultServiceError, set_env_entry

    try:
        result = set_env_entry(data)
    except EnvVaultServiceError as exc:
        await emit_command_error(session, "env.set", exc)
        return True
    await session._send_ws_payload(
        {"type": "env.list", "entries": result.entries},
        log_context="env.list",
    )
    return True


async def handle_env_delete(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.env_vault_service import EnvVaultServiceError, delete_env_entry

    try:
        result = delete_env_entry(data)
    except EnvVaultServiceError as exc:
        await emit_command_error(session, "env.delete", exc)
        return True
    await session._send_ws_payload(
        {"type": "env.list", "entries": result.entries},
        log_context="env.list",
    )
    return True


# ---------------------------------------------------------------------------
# Connectors marketplace handlers
# ---------------------------------------------------------------------------


async def handle_connectors_marketplace_list(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.api.routes_health import get_mcp_manager

    connectors = list_marketplace_connectors(get_mcp_manager())
    await session._send_ws_payload(
        {"type": "connectors.marketplace.list", "connectors": connectors},
        log_context="connectors.marketplace.list",
    )
    return True


async def handle_connectors_marketplace_install(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.api.routes_health import get_mcp_manager

    name = str(data.get("name", "")).strip()
    try:
        result = await install_marketplace_connector(get_mcp_manager(), name)
        refreshed = session.refresh_tool_registry_if_mcp_changed(allow_when_busy=False)
        notice = _tool_registry_availability_notice(session, refreshed) or result["notice"]
        await session._send_event(
            AgentEvent(type="system_notice", data={"content": notice})
        )
        await session._send_ws_payload(
            {"type": "mcp_status", "servers": result["servers"]},
            log_context="mcp_status",
        )
        await session._send_ws_payload(
            {"type": "connectors.marketplace.list", "connectors": result["connectors"]},
            log_context="connectors.marketplace.list",
        )
    except Exception as exc:
        await emit_command_error(session, "connectors.marketplace.install", f"Failed to install connector: {exc}")
    return True


# ---------------------------------------------------------------------------
# Scheduler handlers
# ---------------------------------------------------------------------------


def _get_scheduler(session):
    """Get the shared TaskScheduler from bootstrap state."""
    from backend.services.scheduler_service import get_scheduler_from_bootstrap

    return get_scheduler_from_bootstrap()


def _scheduler_workspace_root(session: "WebSocketSession") -> str | None:
    current_root = getattr(session, "_current_workspace_root", None)
    if not callable(current_root):
        return None
    try:
        return str(current_root() or "")
    except Exception:
        return ""


async def _send_scheduler_snapshot(session: "WebSocketSession", scheduler: Any, *, workspace_root: str | None) -> None:
    from backend.services.scheduler_service import list_scheduled_tasks

    result = list_scheduled_tasks(scheduler, workspace_root=workspace_root)
    await session._send_ws_payload(
        {"type": "scheduler.list", "tasks": result.tasks, "runs": result.runs},
        log_context="scheduler.list",
    )


async def handle_scheduler_list(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    scheduler = _get_scheduler(session)
    await _send_scheduler_snapshot(session, scheduler, workspace_root=_scheduler_workspace_root(session))
    return True


async def handle_scheduler_add(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.scheduler_service import SchedulerServiceError, add_scheduled_task

    scheduler = _get_scheduler(session)
    try:
        result = add_scheduled_task(scheduler, data, workspace_root=_scheduler_workspace_root(session))
    except SchedulerServiceError as exc:
        await emit_command_error(session, "scheduler.add", exc)
        return True
    await session._send_ws_payload(
        {"type": "scheduler.list", "tasks": result.tasks, "runs": result.runs},
        log_context="scheduler.list",
    )
    return True


async def handle_scheduler_remove(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.scheduler_service import SchedulerServiceError, remove_scheduled_task

    scheduler = _get_scheduler(session)
    try:
        result = remove_scheduled_task(scheduler, data, workspace_root=_scheduler_workspace_root(session))
    except SchedulerServiceError as exc:
        await emit_command_error(session, "scheduler.remove", exc)
        return True
    await session._send_ws_payload(
        {"type": "scheduler.list", "tasks": result.tasks, "runs": result.runs},
        log_context="scheduler.list",
    )
    return True


async def handle_scheduler_toggle(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.scheduler_service import SchedulerServiceError, toggle_scheduled_task

    scheduler = _get_scheduler(session)
    try:
        result = toggle_scheduled_task(scheduler, data, workspace_root=_scheduler_workspace_root(session))
    except SchedulerServiceError as exc:
        await emit_command_error(session, "scheduler.toggle", exc)
        return True
    await session._send_ws_payload(
        {"type": "scheduler.list", "tasks": result.tasks, "runs": result.runs},
        log_context="scheduler.list",
    )
    return True


async def handle_mcp_oauth_login(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.api.routes_health import get_mcp_manager

    try:
        servers = await login_mcp_server(get_mcp_manager(), data.get("name", ""))
        session.refresh_tool_registry_if_mcp_changed(allow_when_busy=False)
    except (MCPServiceError, KeyError) as exc:
        await emit_command_error(session, "mcp.oauth.login", exc)
        return True
    await session._send_ws_payload(
        {"type": "mcp_status", "servers": servers},
        log_context="mcp_status",
    )
    return True


async def handle_scheduler_run_now(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.scheduler_service import SchedulerServiceError, run_scheduled_task_now

    scheduler = _get_scheduler(session)
    workspace_root = _scheduler_workspace_root(session)
    try:
        result = run_scheduled_task_now(scheduler, data, workspace_root=workspace_root)
    except SchedulerServiceError as exc:
        await emit_command_error(session, "scheduler.run_now", exc)
        return True
    await session._send_ws_payload(
        {"type": "scheduler.list", "tasks": result.tasks, "runs": result.runs},
        log_context="scheduler.list",
    )
    return True


async def handle_scheduler_retry(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.scheduler_service import SchedulerServiceError, retry_scheduled_task_run

    scheduler = _get_scheduler(session)
    workspace_root = _scheduler_workspace_root(session)
    try:
        result = retry_scheduled_task_run(scheduler, data, workspace_root=workspace_root)
    except SchedulerServiceError as exc:
        await emit_command_error(session, "scheduler.retry", exc)
        return True
    await session._send_ws_payload(
        {"type": "scheduler.list", "tasks": result.tasks, "runs": result.runs},
        log_context="scheduler.list",
    )
    return True


async def handle_scheduler_cancel(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.scheduler_service import SchedulerServiceError, cancel_scheduled_task_run

    scheduler = _get_scheduler(session)
    workspace_root = _scheduler_workspace_root(session)
    try:
        result = cancel_scheduled_task_run(scheduler, data, workspace_root=workspace_root)
    except SchedulerServiceError as exc:
        await emit_command_error(session, "scheduler.cancel", exc)
        return True
    await session._send_ws_payload(
        {"type": "scheduler.list", "tasks": result.tasks, "runs": result.runs},
        log_context="scheduler.list",
    )
    return True


# ---------------------------------------------------------------------------
# Handler dispatch table
# ---------------------------------------------------------------------------

HANDLERS: dict[str, Any] = {
    "mcp.list": handle_mcp_list,
    "mcp.add": handle_mcp_add,
    "mcp.remove": handle_mcp_remove,
    "mcp.restart": handle_mcp_restart,
    "mcp.oauth.login": handle_mcp_oauth_login,
    "env.list": handle_env_list,
    "env.set": handle_env_set,
    "env.delete": handle_env_delete,
    "connectors.marketplace.list": handle_connectors_marketplace_list,
    "connectors.marketplace.install": handle_connectors_marketplace_install,
    "scheduler.list": handle_scheduler_list,
    "scheduler.add": handle_scheduler_add,
    "scheduler.remove": handle_scheduler_remove,
    "scheduler.toggle": handle_scheduler_toggle,
    "scheduler.run_now": handle_scheduler_run_now,
    "scheduler.retry": handle_scheduler_retry,
    "scheduler.cancel": handle_scheduler_cancel,
}
