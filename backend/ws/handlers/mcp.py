from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

from backend.agent.message import AgentEvent

if TYPE_CHECKING:
    from backend.ws.handler import WebSocketSession

logger = logging.getLogger(__name__)


async def handle_mcp_list(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.main import get_mcp_manager

    manager = get_mcp_manager()
    if manager is None:
        await session._send_event(AgentEvent.error("MCP manager not available", recoverable=True))
        return True
    await session._send_ws_payload(
        {"type": "mcp_status", "servers": manager.get_all_status()},
        log_context="mcp_status",
    )
    return True


async def handle_mcp_add(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.main import get_mcp_manager
    from backend.mcp.manager import MCPServerConfig

    manager = get_mcp_manager()
    if manager is None:
        await session._send_event(AgentEvent.error("MCP manager not available", recoverable=True))
        return True

    name = str(data.get("name", "")).strip()
    if not name:
        await session._send_event(AgentEvent.error("Server name is required", recoverable=True))
        return True

    transport = str(data.get("transport", "stdio")).strip().lower()
    config = MCPServerConfig(
        name=name,
        command=str(data.get("command", "python")).strip(),
        args=[str(a) for a in (data.get("args") or [])],
        transport=transport,
        url=str(data.get("url", "")).strip() or None,
        source="user",
    )
    await manager.start_server(config)
    await session._send_ws_payload(
        {"type": "mcp_status", "servers": manager.get_all_status()},
        log_context="mcp_status",
    )
    return True


async def handle_mcp_remove(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.main import get_mcp_manager

    manager = get_mcp_manager()
    if manager is None:
        await session._send_event(AgentEvent.error("MCP manager not available", recoverable=True))
        return True
    name = str(data.get("name", "")).strip()
    if not name:
        await session._send_event(AgentEvent.error("Server name is required", recoverable=True))
        return True
    await manager.stop_server(name)
    await session._send_ws_payload(
        {"type": "mcp_status", "servers": manager.get_all_status()},
        log_context="mcp_status",
    )
    return True


async def handle_mcp_restart(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.main import get_mcp_manager

    manager = get_mcp_manager()
    if manager is None:
        await session._send_event(AgentEvent.error("MCP manager not available", recoverable=True))
        return True
    name = str(data.get("name", "")).strip()
    if not name:
        await session._send_event(AgentEvent.error("Server name is required", recoverable=True))
        return True
    await manager.restart_server(name)
    await session._send_ws_payload(
        {"type": "mcp_status", "servers": manager.get_all_status()},
        log_context="mcp_status",
    )
    return True


# ---------------------------------------------------------------------------
# Env vault handlers
# ---------------------------------------------------------------------------


async def handle_env_list(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.vault import EnvVault

    vault = EnvVault()
    await session._send_ws_payload(
        {"type": "env.list", "entries": vault.list_names()},
        log_context="env.list",
    )
    return True


async def handle_env_set(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.vault import EnvVault

    name = str(data.get("name", "")).strip()
    if not name:
        await session._send_event(AgentEvent.error("Variable name is required", recoverable=True))
        return True
    value = data.get("value")
    if value is None:
        await session._send_event(AgentEvent.error("Variable value is required", recoverable=True))
        return True
    description = str(data.get("description", ""))
    scope = str(data.get("scope", "global"))

    vault = EnvVault()
    vault.set(name, str(value), description=description, scope=scope)
    await session._send_ws_payload(
        {"type": "env.list", "entries": vault.list_names()},
        log_context="env.list",
    )
    return True


async def handle_env_delete(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.vault import EnvVault

    name = str(data.get("name", "")).strip()
    if not name:
        await session._send_event(AgentEvent.error("Variable name is required", recoverable=True))
        return True

    vault = EnvVault()
    deleted = vault.delete(name)
    if not deleted:
        await session._send_event(AgentEvent.error(f"Variable '{name}' not found", recoverable=True))
        return True
    await session._send_ws_payload(
        {"type": "env.list", "entries": vault.list_names()},
        log_context="env.list",
    )
    return True


# ---------------------------------------------------------------------------
# Connectors marketplace handlers
# ---------------------------------------------------------------------------


async def handle_connectors_marketplace_list(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    import json as _json
    from backend.mcp.marketplace import get_marketplace_connectors
    from backend.mcp.config_file import read_mcp_config
    from backend.main import get_mcp_manager

    installed_names: set[str] = set()
    config = read_mcp_config()
    try:
        config_data = _json.loads(str(config.get("content") or "{}"))
        installed_names.update(config_data.get("mcpServers", {}).keys())
    except Exception:
        pass
    manager = get_mcp_manager()
    if manager is not None:
        for s in manager.get_all_status():
            if s.get("name"):
                installed_names.add(s["name"])

    connectors = get_marketplace_connectors(sorted(installed_names))
    await session._send_ws_payload(
        {"type": "connectors.marketplace.list", "connectors": connectors},
        log_context="connectors.marketplace.list",
    )
    return True


async def handle_connectors_marketplace_install(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    import json
    from backend.mcp.config_file import read_mcp_config, write_mcp_config
    from backend.mcp.marketplace import get_marketplace_connectors
    from backend.mcp.manager import MCPServerConfig, MCP_CONFIG_FILE
    from backend.main import get_mcp_manager

    name = str(data.get("name", "")).strip()
    if not name:
        await session._send_event(AgentEvent.error("Connector name is required", recoverable=True))
        return True

    all_connectors = get_marketplace_connectors()
    template = next((c for c in all_connectors if c["name"] == name), None)
    if not template:
        await session._send_event(AgentEvent.error(f"Connector '{name}' not found in marketplace", recoverable=True))
        return True

    mgr = get_mcp_manager()
    if mgr is None:
        await session._send_event(AgentEvent.error("MCP manager not available", recoverable=True))
        return True

    config = MCPServerConfig(
        name=str(template["name"]),
        command=str(template.get("command") or "python"),
        args=[str(arg) for arg in (template.get("args") or [])],
        transport=str(template.get("transport") or "stdio"),
        url=str(template.get("url") or "").strip() or None,
        source="marketplace",
    )

    try:
        current = read_mcp_config()
        current_data = json.loads(str(current.get("content") or "{}"))
        servers_data = current_data.setdefault("mcpServers", {})
        servers_data[config.name] = {
            "command": config.command,
            "args": config.args,
            "env": dict(template.get("env") or {}),
            "autoStart": True,
            "transport": config.transport,
            **({"url": config.url} if config.url else {}),
        }
        write_mcp_config(json.dumps(current_data, ensure_ascii=False, indent=2) + "\n", MCP_CONFIG_FILE)

        await mgr.start_server(config)
        servers = mgr.get_all_status()
        connectors = get_marketplace_connectors([s.get("name", "") for s in servers if s.get("name")])
        status = next((s for s in servers if s.get("name") == config.name), {})
        if status.get("status") == "connected":
            await session._send_event(
                AgentEvent(type="system_notice", data={"content": f"Connector '{config.name}' installed and ready"})
            )
        else:
            await session._send_event(
                AgentEvent(
                    type="system_notice",
                    data={
                        "content": (
                            f"Connector '{config.name}' was saved, but is not ready: "
                            f"{status.get('error') or status.get('status') or 'unknown status'}"
                        )
                    },
                )
            )
        await session._send_ws_payload(
            {"type": "mcp_status", "servers": servers},
            log_context="mcp_status",
        )
        await session._send_ws_payload(
            {"type": "connectors.marketplace.list", "connectors": connectors},
            log_context="connectors.marketplace.list",
        )
    except Exception as exc:
        await session._send_event(AgentEvent.error(f"Failed to install connector: {exc}", recoverable=True))
    return True


# ---------------------------------------------------------------------------
# Scheduler handlers
# ---------------------------------------------------------------------------


async def handle_scheduler_list(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.tasks.scheduler import TaskScheduler

    scheduler = TaskScheduler()
    await session._send_ws_payload(
        {"type": "scheduler.list", "tasks": scheduler.list_tasks()},
        log_context="scheduler.list",
    )
    return True


async def handle_scheduler_add(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.tasks.scheduler import TaskScheduler

    name = str(data.get("name", "")).strip()
    if not name:
        await session._send_event(AgentEvent.error("Task name is required", recoverable=True))
        return True
    prompt = str(data.get("prompt", "")).strip()
    if not prompt:
        await session._send_event(AgentEvent.error("Task prompt is required", recoverable=True))
        return True
    schedule = str(data.get("schedule", "")).strip()
    if not schedule:
        await session._send_event(AgentEvent.error("Task schedule is required", recoverable=True))
        return True
    permission_mode = str(data.get("permission_mode", "auto_approve"))

    scheduler = TaskScheduler()
    scheduler.add_task(name=name, prompt=prompt, schedule=schedule, permission_mode=permission_mode)
    await session._send_ws_payload(
        {"type": "scheduler.list", "tasks": scheduler.list_tasks()},
        log_context="scheduler.list",
    )
    return True


async def handle_scheduler_remove(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.tasks.scheduler import TaskScheduler

    task_id = str(data.get("task_id") or data.get("id") or "").strip()
    if not task_id:
        await session._send_event(AgentEvent.error("Task ID is required", recoverable=True))
        return True

    scheduler = TaskScheduler()
    changed = scheduler.remove_task(task_id)
    if not changed:
        await session._send_event(AgentEvent.error(f"Task '{task_id}' not found", recoverable=True))
        return True
    await session._send_ws_payload(
        {"type": "scheduler.list", "tasks": scheduler.list_tasks()},
        log_context="scheduler.list",
    )
    return True


async def handle_scheduler_toggle(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.tasks.scheduler import TaskScheduler

    task_id = str(data.get("task_id") or data.get("id") or "").strip()
    if not task_id:
        await session._send_event(AgentEvent.error("Task ID is required", recoverable=True))
        return True

    scheduler = TaskScheduler()
    changed = scheduler.toggle_task(task_id, bool(data.get("enabled", True)))
    if not changed:
        await session._send_event(AgentEvent.error(f"Task '{task_id}' not found", recoverable=True))
        return True
    await session._send_ws_payload(
        {"type": "scheduler.list", "tasks": scheduler.list_tasks()},
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
