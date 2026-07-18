from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

from backend.hooks.runtime import run_config_change_hook
from backend.mcp import config_file as config_file_mod
from backend.mcp.manager import MCP_CONFIG_FILE, MCPServerConfig
from backend.mcp.marketplace import get_marketplace_connectors

ConfigChangeHook = Callable[..., Awaitable[Any]]


class MCPServiceError(ValueError):
    """User-recoverable MCP operation failure."""


def require_mcp_manager(manager: Any | None) -> Any:
    if manager is None:
        raise MCPServiceError("MCP manager not available")
    return manager


def get_mcp_status(manager: Any | None) -> list[dict[str, Any]]:
    manager = require_mcp_manager(manager)
    return list(manager.get_all_status())


async def add_mcp_server(
    manager: Any | None,
    data: dict[str, Any],
    *,
    config_change_hook: ConfigChangeHook = run_config_change_hook,
) -> list[dict[str, Any]]:
    manager = require_mcp_manager(manager)
    config = _manual_config_from_payload(data)
    await _upsert_mcp_config(config.name, _server_entry_from_config(config), config_change_hook=config_change_hook)
    await manager.start_server(config)
    return list(manager.get_all_status())


async def remove_mcp_server(
    manager: Any | None,
    name: str,
    *,
    config_change_hook: ConfigChangeHook = run_config_change_hook,
) -> list[dict[str, Any]]:
    manager = require_mcp_manager(manager)
    server_name = _required_name(name, "Server name is required")
    await manager.remove_server(server_name)
    current_data = _read_current_config_data()
    servers_data = current_data.setdefault("mcpServers", {})
    if isinstance(servers_data, dict):
        servers_data.pop(server_name, None)
        await _write_config_data(current_data, config_change_hook=config_change_hook)
    return list(manager.get_all_status())


async def restart_mcp_server(manager: Any | None, name: str) -> list[dict[str, Any]]:
    manager = require_mcp_manager(manager)
    server_name = _required_name(name, "Server name is required")
    await manager.restart_server(server_name)
    return list(manager.get_all_status())


def list_marketplace_connectors(manager: Any | None = None) -> list[dict[str, Any]]:
    return get_marketplace_connectors(sorted(_installed_connector_names(manager)))


async def install_marketplace_connector(
    manager: Any | None,
    name: str,
    *,
    config_change_hook: ConfigChangeHook = run_config_change_hook,
) -> dict[str, Any]:
    manager = require_mcp_manager(manager)
    connector_name = _required_name(name, "Connector name is required")
    template = next((item for item in get_marketplace_connectors() if item["name"] == connector_name), None)
    if not template:
        raise MCPServiceError(f"Connector '{connector_name}' not found in marketplace")

    config = _marketplace_config_from_template(template)
    await _upsert_mcp_config(config.name, _server_entry_from_template(config, template), config_change_hook=config_change_hook)

    if config.auto_start:
        await manager.start_server(config)
    else:
        await manager.register_config(config)

    servers = list(manager.get_all_status())
    connectors = get_marketplace_connectors([str(item.get("name", "")) for item in servers if item.get("name")])
    status = next((item for item in servers if item.get("name") == config.name), {})
    if status.get("status") == "connected":
        notice = f"Connector '{config.name}' installed and ready"
    else:
        notice = (
            f"Connector '{config.name}' was saved, but is not ready: "
            f"{status.get('error') or status.get('status') or 'unknown status'}"
        )
    return {
        "servers": servers,
        "connectors": connectors,
        "notice": notice,
        "status": status,
    }


def _manual_config_from_payload(data: dict[str, Any]) -> MCPServerConfig:
    name = _required_name(data.get("name", ""), "Server name is required")
    transport = str(data.get("transport", "stdio")).strip().lower()
    return MCPServerConfig(
        name=name,
        command=str(data.get("command", "python")).strip(),
        args=[str(item) for item in (data.get("args") or [])],
        env={str(key): str(value) for key, value in dict(data.get("env") or {}).items()},
        transport=transport,
        url=str(data.get("url", "")).strip() or None,
        source="user",
    )


def _marketplace_config_from_template(template: dict[str, Any]) -> MCPServerConfig:
    transport = str(template.get("transport") or "stdio")
    return MCPServerConfig(
        name=str(template["name"]),
        command=str(template.get("command") or ("python" if transport == "stdio" else "")),
        args=[str(arg) for arg in (template.get("args") or [])],
        transport=transport,
        url=str(template.get("url") or "").strip() or None,
        env={str(key): str(value) for key, value in dict(template.get("env") or {}).items()},
        auto_start=bool(template.get("autoStart", True)),
        max_retries=int(template.get("maxRetries", 3)),
        source="marketplace",
        requires_user_action=bool(template.get("requiresUserAction", False)),
        setup_hint=str(template.get("setupHint") or ""),
        docs_url=str(template.get("docsUrl") or ""),
    )


def _server_entry_from_config(config: MCPServerConfig) -> dict[str, Any]:
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
    return server_entry


def _server_entry_from_template(config: MCPServerConfig, template: dict[str, Any]) -> dict[str, Any]:
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
    return server_entry


async def _upsert_mcp_config(
    name: str,
    server_entry: dict[str, Any],
    *,
    config_change_hook: ConfigChangeHook,
) -> None:
    current_data = _read_current_config_data()
    servers_data = current_data.setdefault("mcpServers", {})
    if not isinstance(servers_data, dict):
        raise MCPServiceError("MCP config must contain an object field named 'mcpServers'.")
    servers_data[name] = server_entry
    await _write_config_data(current_data, config_change_hook=config_change_hook)


def _read_current_config_data() -> dict[str, Any]:
    current = config_file_mod.read_mcp_config()
    content = str(current.get("content") or "{}")
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        raise MCPServiceError(f"Invalid MCP config JSON: {exc.msg}") from exc
    if not isinstance(parsed, dict):
        raise MCPServiceError("MCP config must be a JSON object.")
    return parsed


async def _write_config_data(
    data: dict[str, Any],
    *,
    config_change_hook: ConfigChangeHook,
) -> None:
    config_file_mod.write_mcp_config(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        MCP_CONFIG_FILE,
    )
    await config_change_hook(source="mcp", file_path=str(MCP_CONFIG_FILE))


def _installed_connector_names(manager: Any | None) -> set[str]:
    installed_names: set[str] = set()
    try:
        config = config_file_mod.read_mcp_config()
        config_data = json.loads(str(config.get("content") or "{}"))
        servers = config_data.get("mcpServers", {}) if isinstance(config_data, dict) else {}
        if isinstance(servers, dict):
            installed_names.update(str(name) for name in servers.keys())
    except Exception:
        pass
    if manager is not None:
        for status in manager.get_all_status():
            if status.get("name"):
                installed_names.add(str(status["name"]))
    return installed_names


def _required_name(value: Any, message: str) -> str:
    name = str(value or "").strip()
    if not name:
        raise MCPServiceError(message)
    return name
