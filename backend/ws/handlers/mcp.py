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
    remove_mcp_server,
    restart_mcp_server,
)
from backend.ws.command_results import emit_command_error

if TYPE_CHECKING:
    from backend.ws.handler import WebSocketSession

logger = logging.getLogger(__name__)


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
    return True


async def handle_mcp_add(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.api.routes_health import get_mcp_manager

    try:
        servers = await add_mcp_server(get_mcp_manager(), data)
        session.refresh_tool_registry_if_mcp_changed(allow_when_busy=False)
    except Exception as exc:
        await emit_command_error(session, "mcp.add", f"Failed to add MCP server: {exc}")
        return True
    await session._send_ws_payload(
        {"type": "mcp_status", "servers": servers},
        log_context="mcp_status",
    )
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
        session.refresh_tool_registry_if_mcp_changed(allow_when_busy=False)
        await session._send_event(
            AgentEvent(type="system_notice", data={"content": result["notice"]})
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


async def handle_scheduler_list(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.scheduler_service import list_scheduled_tasks

    scheduler = _get_scheduler(session)
    await session._send_ws_payload(
        {"type": "scheduler.list", "tasks": list_scheduled_tasks(scheduler).tasks},
        log_context="scheduler.list",
    )
    return True


async def handle_scheduler_add(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.scheduler_service import SchedulerServiceError, add_scheduled_task

    scheduler = _get_scheduler(session)
    try:
        result = add_scheduled_task(scheduler, data)
    except SchedulerServiceError as exc:
        await emit_command_error(session, "scheduler.add", exc)
        return True
    await session._send_ws_payload(
        {"type": "scheduler.list", "tasks": result.tasks},
        log_context="scheduler.list",
    )
    return True


async def handle_scheduler_remove(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.scheduler_service import SchedulerServiceError, remove_scheduled_task

    scheduler = _get_scheduler(session)
    try:
        result = remove_scheduled_task(scheduler, data)
    except SchedulerServiceError as exc:
        await emit_command_error(session, "scheduler.remove", exc)
        return True
    await session._send_ws_payload(
        {"type": "scheduler.list", "tasks": result.tasks},
        log_context="scheduler.list",
    )
    return True


async def handle_scheduler_toggle(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.scheduler_service import SchedulerServiceError, toggle_scheduled_task

    scheduler = _get_scheduler(session)
    try:
        result = toggle_scheduled_task(scheduler, data)
    except SchedulerServiceError as exc:
        await emit_command_error(session, "scheduler.toggle", exc)
        return True
    await session._send_ws_payload(
        {"type": "scheduler.list", "tasks": result.tasks},
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
    "env.list": handle_env_list,
    "env.set": handle_env_set,
    "env.delete": handle_env_delete,
    "connectors.marketplace.list": handle_connectors_marketplace_list,
    "connectors.marketplace.install": handle_connectors_marketplace_install,
    "scheduler.list": handle_scheduler_list,
    "scheduler.add": handle_scheduler_add,
    "scheduler.remove": handle_scheduler_remove,
    "scheduler.toggle": handle_scheduler_toggle,
}
