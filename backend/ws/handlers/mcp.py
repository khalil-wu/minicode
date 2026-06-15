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
    import json
    from backend.main import get_mcp_manager
    from backend.mcp.config_file import read_mcp_config, write_mcp_config
    from backend.mcp.manager import MCPServerConfig
    from backend.mcp.manager import MCP_CONFIG_FILE

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
        env={str(key): str(value) for key, value in dict(data.get("env") or {}).items()},
        transport=transport,
        url=str(data.get("url", "")).strip() or None,
        source="user",
    )
    try:
        current = read_mcp_config()
        current_data = json.loads(str(current.get("content") or "{}"))
        servers_data = current_data.setdefault("mcpServers", {})
        server_entry: dict[str, Any] = {
            "transport": config.transport,
            "autoStart": config.auto_start,
        }
        if config.transport == "stdio":
            server_entry["command"] = config.command
            server_entry["args"] = config.args
        if config.env:
            server_entry["env"] = config.env
        if config.url:
            server_entry["url"] = config.url
        servers_data[config.name] = server_entry
        write_mcp_config(json.dumps(current_data, ensure_ascii=False, indent=2) + "\n", MCP_CONFIG_FILE)

        await manager.start_server(config)
        session.refresh_tool_registry_if_mcp_changed(allow_when_busy=False)
    except Exception as exc:
        await session._send_event(AgentEvent.error(f"Failed to add MCP server: {exc}", recoverable=True))
        return True
    await session._send_ws_payload(
        {"type": "mcp_status", "servers": manager.get_all_status()},
        log_context="mcp_status",
    )
    return True


async def handle_mcp_remove(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    import json
    from backend.main import get_mcp_manager
    from backend.mcp.config_file import read_mcp_config, write_mcp_config
    from backend.mcp.manager import MCP_CONFIG_FILE

    manager = get_mcp_manager()
    if manager is None:
        await session._send_event(AgentEvent.error("MCP manager not available", recoverable=True))
        return True
    name = str(data.get("name", "")).strip()
    if not name:
        await session._send_event(AgentEvent.error("Server name is required", recoverable=True))
        return True
    await manager.remove_server(name)
    current = read_mcp_config()
    current_data = json.loads(str(current.get("content") or "{}"))
    servers_data = current_data.setdefault("mcpServers", {})
    if isinstance(servers_data, dict):
        servers_data.pop(name, None)
        write_mcp_config(json.dumps(current_data, ensure_ascii=False, indent=2) + "\n", MCP_CONFIG_FILE)
    session.refresh_tool_registry_if_mcp_changed(allow_when_busy=False)
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
    session.refresh_tool_registry_if_mcp_changed(allow_when_busy=False)
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
        command=str(template.get("command") or ("python" if str(template.get("transport") or "stdio") == "stdio" else "")),
        args=[str(arg) for arg in (template.get("args") or [])],
        transport=str(template.get("transport") or "stdio"),
        url=str(template.get("url") or "").strip() or None,
        env={str(key): str(value) for key, value in dict(template.get("env") or {}).items()},
        auto_start=bool(template.get("autoStart", True)),
        max_retries=int(template.get("maxRetries", 3)),
        source="marketplace",
        requires_user_action=bool(template.get("requiresUserAction", False)),
        setup_hint=str(template.get("setupHint") or ""),
        docs_url=str(template.get("docsUrl") or ""),
    )

    try:
        current = read_mcp_config()
        current_data = json.loads(str(current.get("content") or "{}"))
        servers_data = current_data.setdefault("mcpServers", {})
        server_entry: dict[str, Any] = {
            "env": dict(template.get("env") or {}),
            "autoStart": config.auto_start,
            "transport": config.transport,
        }
        if config.max_retries != 3:
            server_entry["maxRetries"] = config.max_retries
        if config.transport == "stdio":
            server_entry["command"] = config.command
            server_entry["args"] = config.args
        elif config.args:
            server_entry["args"] = config.args
        if config.url:
            server_entry["url"] = config.url
        if template.get("requiresUserAction"):
            server_entry["requiresUserAction"] = True
        if template.get("setupHint"):
            server_entry["setupHint"] = str(template["setupHint"])
        if template.get("docsUrl"):
            server_entry["docsUrl"] = str(template["docsUrl"])
        servers_data[config.name] = server_entry
        write_mcp_config(json.dumps(current_data, ensure_ascii=False, indent=2) + "\n", MCP_CONFIG_FILE)

        if config.auto_start:
            await mgr.start_server(config)
        else:
            await mgr.register_config(config)
        session.refresh_tool_registry_if_mcp_changed(allow_when_busy=False)
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


def _get_scheduler(session):
    """Get the shared TaskScheduler from bootstrap state."""
    from backend.api import _state as api_state
    bootstrap = api_state.bootstrap
    if bootstrap and getattr(bootstrap, 'task_scheduler', None):
        return bootstrap.task_scheduler
    from backend.tasks.scheduler import get_global_scheduler
    return get_global_scheduler()


async def handle_scheduler_list(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    scheduler = _get_scheduler(session)
    await session._send_ws_payload(
        {"type": "scheduler.list", "tasks": scheduler.list_tasks()},
        log_context="scheduler.list",
    )
    return True


async def handle_scheduler_add(session: "WebSocketSession", data: dict[str, Any]) -> bool:
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

    scheduler = _get_scheduler(session)
    scheduler.add_task(name=name, prompt=prompt, schedule=schedule, permission_mode=permission_mode)
    await session._send_ws_payload(
        {"type": "scheduler.list", "tasks": scheduler.list_tasks()},
        log_context="scheduler.list",
    )
    return True


async def handle_scheduler_remove(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    task_id = str(data.get("task_id") or data.get("id") or "").strip()
    if not task_id:
        await session._send_event(AgentEvent.error("Task ID is required", recoverable=True))
        return True

    scheduler = _get_scheduler(session)
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
    task_id = str(data.get("task_id") or data.get("id") or "").strip()
    if not task_id:
        await session._send_event(AgentEvent.error("Task ID is required", recoverable=True))
        return True

    scheduler = _get_scheduler(session)
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
