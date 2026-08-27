from __future__ import annotations

import asyncio
import json
import math
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Awaitable, Callable

from mcp.shared.exceptions import McpError

from backend.hooks.runtime import (
    ConfigChangeHookBlocked,
    raise_if_config_change_blocked,
    run_config_change_hook,
)
from backend.mcp import config_file as config_file_mod
from backend.mcp.client import MCPAuthenticationError
from backend.mcp.manager import (
    MCP_CONFIG_FILE,
    MCP_REQUEST_TIMEOUT_SECONDS,
    MCPServerConfig,
    validate_mcp_server_config,
)
from backend.mcp.oauth import MCPAuthenticationRequired
from backend.mcp.project_settings import (
    PROJECT_MCP_APPROVED,
    approve_project_mcp_server,
    project_local_settings_path,
    reject_project_mcp_server,
)
from backend.mcp.transport import normalize_mcp_transport

ConfigChangeHook = Callable[..., Awaitable[Any]]
_MCP_CONFIG_MUTATION_LOCK = asyncio.Lock()


class MCPServiceError(ValueError):
    """User-recoverable MCP operation failure."""


class MCPInventoryServiceError(MCPServiceError):
    """Typed failure for the user-initiated MCP inventory browser."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        recoverable: bool,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.recoverable = recoverable
        self.details = dict(details or {})


def _assert_config_change_allowed(
    result: Any | None,
    *,
    source: str,
    file_path: str,
) -> None:
    try:
        raise_if_config_change_blocked(result, source=source, file_path=file_path)
    except ConfigChangeHookBlocked as exc:
        raise MCPServiceError(str(exc)) from exc


def require_mcp_manager(manager: Any | None) -> Any:
    if manager is None:
        raise MCPServiceError("MCP manager not available")
    return manager


def _iter_inventory_exceptions(exc: BaseException):
    yield exc
    if isinstance(exc, BaseExceptionGroup):
        for nested in exc.exceptions:
            yield from _iter_inventory_exceptions(nested)
    cause = exc.__cause__ or exc.__context__
    if cause is not None and cause is not exc:
        yield from _iter_inventory_exceptions(cause)


def _inventory_failure(exc: BaseException) -> MCPInventoryServiceError:
    for item in _iter_inventory_exceptions(exc):
        if isinstance(item, (MCPAuthenticationRequired, MCPAuthenticationError)):
            expired = bool(getattr(item, "mcp_auth_expired", False))
            return MCPInventoryServiceError(
                "MCP authentication has expired" if expired else "MCP authentication is required",
                code="authentication_expired" if expired else "authentication_required",
                recoverable=False,
            )
        if isinstance(item, (TimeoutError, asyncio.TimeoutError)):
            return MCPInventoryServiceError(
                "MCP inventory request timed out",
                code="timeout",
                recoverable=True,
            )
        if isinstance(item, McpError):
            code = int(item.error.code)
            if code in {401, 403, -32001}:
                return MCPInventoryServiceError(
                    "MCP authentication is required",
                    code="authentication_required",
                    recoverable=False,
                    details={"mcp_code": code},
                )
            if code == 408:
                return MCPInventoryServiceError(
                    "MCP inventory request timed out",
                    code="timeout",
                    recoverable=True,
                    details={"mcp_code": code},
                )
            return MCPInventoryServiceError(
                f"MCP protocol error: {item.error.message}",
                code="protocol_error",
                recoverable=True,
                details={"mcp_code": code},
            )
        if isinstance(item, ConnectionError):
            return MCPInventoryServiceError(
                str(item) or "MCP connection failed",
                code="transport_error",
                recoverable=True,
            )
    return MCPInventoryServiceError(
        str(exc) or "MCP inventory request failed",
        code="protocol_error",
        recoverable=True,
    )


async def list_mcp_inventory(manager: Any | None, name: str) -> dict[str, Any]:
    """List standard MCP resources, templates and prompts on explicit demand.

    This deliberately does no work during startup, ordinary status refreshes,
    or Agent turns. The three independent MCP list methods run concurrently
    only after the user expands one connected server in the connector UI.
    """

    manager = require_mcp_manager(manager)
    server_name = _required_name(name, "Server name is required")
    client = manager.get_client(server_name)
    if client is None:
        lifecycle = manager.get_server_lifecycle(server_name)
        if lifecycle is None:
            raise MCPInventoryServiceError(
                f"MCP server '{server_name}' is not configured",
                code="server_not_found",
                recoverable=False,
            )
        phase = str(lifecycle.get("phase") or "")
        if phase in {"auth_required", "expired"}:
            raise MCPInventoryServiceError(
                "MCP authentication has expired" if phase == "expired" else "MCP authentication is required",
                code="authentication_expired" if phase == "expired" else "authentication_required",
                recoverable=False,
                details={"phase": phase},
            )
        raise MCPInventoryServiceError(
            f"MCP server '{server_name}' is not connected",
            code="not_connected",
            recoverable=True,
            details={"phase": phase or "stopped"},
        )

    capabilities = getattr(client, "server_capabilities", None)
    if capabilities is None:
        raise MCPInventoryServiceError(
            f"MCP server '{server_name}' has not completed capability negotiation",
            code="capabilities_unavailable",
            recoverable=True,
        )

    async def _empty() -> list[Any]:
        return []

    try:
        resources, templates, prompts = await asyncio.wait_for(
            asyncio.gather(
                client.list_resources() if bool(getattr(capabilities, "resources", False)) else _empty(),
                client.list_resource_templates() if bool(getattr(capabilities, "resources", False)) else _empty(),
                client.list_prompts() if bool(getattr(capabilities, "prompts", False)) else _empty(),
            ),
            timeout=MCP_REQUEST_TIMEOUT_SECONDS,
        )
    except asyncio.CancelledError:
        raise
    except BaseException as exc:
        raise _inventory_failure(exc) from exc

    resource_payload = [
        {
            "uri": item.uri,
            "name": item.name,
            "description": item.description,
            "mime_type": item.mime_type,
        }
        for item in resources
    ]
    template_payload = [
        {
            "uri_template": item.uri_template,
            "name": item.name,
            "description": item.description,
            "mime_type": item.mime_type,
        }
        for item in templates
    ]
    prompt_payload = [
        {
            "name": item.name,
            "description": item.description,
            "arguments": [
                {
                    "name": argument.name,
                    "description": argument.description,
                    "required": argument.required,
                }
                for argument in item.arguments
            ],
        }
        for item in prompts
    ]
    return {
        "server_name": server_name,
        "capabilities": {
            "resources": bool(getattr(capabilities, "resources", False)),
            "resources_subscribe": bool(getattr(capabilities, "resources_subscribe", False)),
            "resources_list_changed": bool(getattr(capabilities, "resources_list_changed", False)),
            "prompts": bool(getattr(capabilities, "prompts", False)),
        },
        "resources": resource_payload,
        "resource_templates": template_payload,
        "prompts": prompt_payload,
        "empty": not (resource_payload or template_payload or prompt_payload),
    }


def get_mcp_status(manager: Any | None) -> list[dict[str, Any]]:
    manager = require_mcp_manager(manager)
    statuses = list(manager.get_all_status())
    servers = _read_current_config_data().get("servers", {})
    if not isinstance(servers, dict):
        raise MCPServiceError("MCP config must contain an object field named 'servers'.")
    for status in statuses:
        if status.get("source") != "user":
            continue
        entry = servers.get(str(status.get("name") or ""))
        if not isinstance(entry, dict):
            continue
        status.update({
            "command": str(entry.get("command") or ""),
            "args": [str(item) for item in (entry.get("args") or [])],
            "env": {str(key): str(value) for key, value in dict(entry.get("env") or {}).items()},
            "env_vars": list(entry.get("env_vars") or []),
            "headers": {str(key): str(value) for key, value in dict(entry.get("headers") or {}).items()},
            "headers_helper": str(entry.get("headers_helper") or ""),
            "oauth": dict(entry.get("oauth") or {}),
            "cwd": str(entry.get("cwd") or ""),
            "url": str(entry.get("url") or ""),
            "auto_start": bool(entry.get("auto_start", True)),
            "startup_timeout_sec": entry.get("startup_timeout_sec"),
            "tool_timeout_sec": entry.get("tool_timeout_sec"),
            "editable": True,
        })
    return statuses


async def add_mcp_server(
    manager: Any | None,
    data: dict[str, Any],
    *,
    config_change_hook: ConfigChangeHook = run_config_change_hook,
) -> list[dict[str, Any]]:
    manager = require_mcp_manager(manager)
    config = _manual_config_from_payload(data)
    _validate_config_for_service(config)
    async with _MCP_CONFIG_MUTATION_LOCK:
        await _upsert_mcp_config(
            config.name,
            _server_entry_from_payload(config, data),
            config_change_hook=config_change_hook,
        )
    # Reconcile the complete scope stack so a user edit cannot bypass a
    # higher-precedence project/dynamic declaration with the same name.
    await manager.reload_config()
    return get_mcp_status(manager)


async def update_mcp_server(
    manager: Any | None,
    data: dict[str, Any],
    *,
    config_change_hook: ConfigChangeHook = run_config_change_hook,
) -> list[dict[str, Any]]:
    manager = require_mcp_manager(manager)
    original_name = _required_name(data.get("original_name") or data.get("name"), "Server name is required")
    config = _manual_config_from_payload(data)
    _validate_config_for_service(config)
    async with _MCP_CONFIG_MUTATION_LOCK:
        current_data = _read_current_config_data()
        servers_data = current_data.setdefault("servers", {})
        if not isinstance(servers_data, dict):
            raise MCPServiceError("MCP config must contain an object field named 'servers'.")
        current_entry = servers_data.get(original_name)
        if not isinstance(current_entry, dict):
            raise MCPServiceError(f"MCP server '{original_name}' is not editable")
        next_entry = _server_entry_from_payload(config, data, existing=current_entry)
        if original_name != config.name:
            servers_data.pop(original_name, None)
        servers_data[config.name] = next_entry
        await _write_config_data(current_data, config_change_hook=config_change_hook)
    if original_name != config.name:
        await manager.remove_server(original_name)
    await manager.reload_config()
    return get_mcp_status(manager)


async def toggle_mcp_server(
    manager: Any | None,
    name: str,
    enabled: bool,
    *,
    config_change_hook: ConfigChangeHook = run_config_change_hook,
) -> list[dict[str, Any]]:
    manager = require_mcp_manager(manager)
    server_name = _required_name(name, "Server name is required")
    if not isinstance(enabled, bool):
        raise MCPServiceError("MCP enabled must be a boolean")
    async with _MCP_CONFIG_MUTATION_LOCK:
        current_data = _read_current_config_data()
        servers_data = current_data.setdefault("servers", {})
        entry = servers_data.get(server_name) if isinstance(servers_data, dict) else None
        if not isinstance(entry, dict):
            raise MCPServiceError(f"MCP server '{server_name}' is not editable")
        entry["auto_start"] = enabled
        await _write_config_data(current_data, config_change_hook=config_change_hook)
    await manager.reload_config()
    return get_mcp_status(manager)


async def remove_mcp_server(
    manager: Any | None,
    name: str,
    *,
    config_change_hook: ConfigChangeHook = run_config_change_hook,
) -> list[dict[str, Any]]:
    manager = require_mcp_manager(manager)
    server_name = _required_name(name, "Server name is required")
    async with _MCP_CONFIG_MUTATION_LOCK:
        current_data = _read_current_config_data()
        servers_data = current_data.setdefault("servers", {})
        if not isinstance(servers_data, dict) or server_name not in servers_data:
            raise MCPServiceError(f"MCP server '{server_name}' is not editable")
        servers_data.pop(server_name)
        await _write_config_data(current_data, config_change_hook=config_change_hook)
    await manager.reload_config()
    return get_mcp_status(manager)


async def restart_mcp_server(manager: Any | None, name: str) -> list[dict[str, Any]]:
    manager = require_mcp_manager(manager)
    server_name = _required_name(name, "Server name is required")
    _require_connectable_server(manager, server_name)
    await manager.restart_server(server_name)
    return get_mcp_status(manager)


async def login_mcp_server(manager: Any | None, name: str) -> list[dict[str, Any]]:
    manager = require_mcp_manager(manager)
    server_name = _required_name(name, "Server name is required")
    _require_connectable_server(manager, server_name)
    await manager.oauth_login(server_name)
    return get_mcp_status(manager)


async def logout_mcp_server(manager: Any | None, name: str) -> list[dict[str, Any]]:
    manager = require_mcp_manager(manager)
    server_name = _required_name(name, "Server name is required")
    _require_connectable_server(manager, server_name)
    await manager.oauth_logout(server_name)
    return get_mcp_status(manager)


async def approve_project_mcp(
    manager: Any | None,
    name: str,
    *,
    workspace_root: str | Path,
    approve_all: bool = False,
    config_change_hook: ConfigChangeHook = run_config_change_hook,
) -> list[dict[str, Any]]:
    manager = require_mcp_manager(manager)
    server_name = _required_name(name, "Server name is required")
    async with _MCP_CONFIG_MUTATION_LOCK:
        project_workspace = _active_project_mcp_workspace(manager, server_name, workspace_root)
        settings_path = project_local_settings_path(project_workspace)
        hook_result = await config_change_hook(source="mcp", file_path=str(settings_path))
        _assert_config_change_allowed(
            hook_result,
            source="mcp",
            file_path=str(settings_path),
        )
        settings_path = approve_project_mcp_server(
            server_name,
            project_workspace,
            approve_all=approve_all,
        )
        await manager.reload_config()
    return get_mcp_status(manager)


async def reject_project_mcp(
    manager: Any | None,
    name: str,
    *,
    workspace_root: str | Path,
    config_change_hook: ConfigChangeHook = run_config_change_hook,
) -> list[dict[str, Any]]:
    manager = require_mcp_manager(manager)
    server_name = _required_name(name, "Server name is required")
    async with _MCP_CONFIG_MUTATION_LOCK:
        project_workspace = _active_project_mcp_workspace(manager, server_name, workspace_root)
        settings_path = project_local_settings_path(project_workspace)
        hook_result = await config_change_hook(source="mcp", file_path=str(settings_path))
        _assert_config_change_allowed(
            hook_result,
            source="mcp",
            file_path=str(settings_path),
        )
        settings_path = reject_project_mcp_server(server_name, project_workspace)
        await manager.reload_config()
    return get_mcp_status(manager)


def _manual_config_from_payload(data: dict[str, Any]) -> MCPServerConfig:
    name = _required_name(data.get("name", ""), "Server name is required")
    if "\x00" in name:
        raise MCPServiceError("MCP server names cannot contain NUL")
    if "transport" not in data:
        raise MCPServiceError("MCP transport is required")
    raw_transport = data.get("transport")
    if not isinstance(raw_transport, str):
        raise MCPServiceError("MCP transport must be a string")
    try:
        transport = normalize_mcp_transport(raw_transport)
    except ValueError as exc:
        raise MCPServiceError(str(exc)) from exc
    raw_command = data.get("command")
    if raw_command is not None and not isinstance(raw_command, str):
        raise MCPServiceError("MCP command must be a string")
    raw_args = data.get("args", [])
    if not isinstance(raw_args, list) or any(not isinstance(item, str) for item in raw_args):
        raise MCPServiceError("MCP args must be an array of strings")
    raw_env = data.get("env", {})
    if not isinstance(raw_env, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in raw_env.items()
    ):
        raise MCPServiceError("MCP env must be an object of string values")
    raw_headers = data.get("headers", {})
    if not isinstance(raw_headers, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in raw_headers.items()
    ):
        raise MCPServiceError("MCP headers must be an object of string values")
    raw_headers_helper = data.get("headers_helper", "")
    if not isinstance(raw_headers_helper, str):
        raise MCPServiceError("MCP headers_helper must be a string")
    raw_oauth = data.get("oauth", {})
    if not isinstance(raw_oauth, dict):
        raise MCPServiceError("MCP oauth must be an object")
    oauth_client_id = raw_oauth.get("client_id", "")
    oauth_callback_port = raw_oauth.get("callback_port")
    if not isinstance(oauth_client_id, str):
        raise MCPServiceError("MCP oauth.client_id must be a string")
    if oauth_callback_port is not None and (
        not isinstance(oauth_callback_port, int)
        or isinstance(oauth_callback_port, bool)
    ):
        raise MCPServiceError("MCP oauth.callback_port must be an integer")
    raw_cwd = data.get("cwd")
    if raw_cwd is not None and not isinstance(raw_cwd, str):
        raise MCPServiceError("MCP cwd must be a string")
    raw_url = data.get("url")
    if raw_url is not None and not isinstance(raw_url, str):
        raise MCPServiceError("MCP url must be a string")
    raw_auto_start = data.get("auto_start", True)
    if not isinstance(raw_auto_start, bool):
        raise MCPServiceError("MCP auto_start must be a boolean")
    startup_timeout_sec = _optional_positive_timeout(
        data.get("startup_timeout_sec"),
        "startup_timeout_sec",
    )
    tool_timeout_sec = _optional_positive_timeout(
        data.get("tool_timeout_sec"),
        "tool_timeout_sec",
    )
    required = data.get("required", False)
    supports_parallel_tool_calls = data.get("supports_parallel_tool_calls", False)
    if not isinstance(required, bool) or not isinstance(supports_parallel_tool_calls, bool):
        raise MCPServiceError("MCP lifecycle policy fields must be booleans")
    enabled_tools = _optional_tool_names(data.get("enabled_tools"), preserve_none=True)
    disabled_tools = _optional_tool_names(data.get("disabled_tools"), preserve_none=False) or []
    default_tools_approval_mode = _optional_approval_mode(
        data.get("default_tools_approval_mode"),
        "default_tools_approval_mode",
    )
    tool_approval_modes = _tool_approval_modes(data.get("tools"))
    return MCPServerConfig(
        name=name,
        command=raw_command.strip() if isinstance(raw_command, str) else "",
        args=list(raw_args),
        env=dict(raw_env),
        headers=dict(raw_headers),
        headers_helper=raw_headers_helper.strip(),
        oauth_client_id=oauth_client_id.strip(),
        oauth_callback_port=oauth_callback_port,
        cwd=raw_cwd.strip() or None if isinstance(raw_cwd, str) else None,
        transport=transport,
        url=raw_url.strip() or None if isinstance(raw_url, str) else None,
        auto_start=raw_auto_start,
        source="user",
        startup_timeout_sec=startup_timeout_sec,
        tool_timeout_sec=tool_timeout_sec,
        required=required,
        supports_parallel_tool_calls=supports_parallel_tool_calls,
        enabled_tools=enabled_tools,
        disabled_tools=disabled_tools,
        default_tools_approval_mode=default_tools_approval_mode,
        tool_approval_modes=tool_approval_modes,
    )


def _validate_config_for_service(config: MCPServerConfig) -> None:
    try:
        validate_mcp_server_config(config)
    except ValueError as exc:
        raise MCPServiceError(f"Invalid MCP server configuration: {exc}") from exc


def _server_entry_from_config(config: MCPServerConfig) -> dict[str, Any]:
    server_entry: dict[str, Any] = {
        "transport": config.transport,
        "auto_start": config.auto_start,
    }
    if config.transport == "stdio":
        server_entry["command"] = config.command
        server_entry["args"] = config.args
        if config.env:
            server_entry["env"] = config.env
        if config.cwd:
            server_entry["cwd"] = config.cwd
    else:
        server_entry["url"] = config.url
        if config.headers:
            server_entry["headers"] = config.headers
        if config.headers_helper:
            server_entry["headers_helper"] = config.headers_helper
        if config.transport in {"sse", "http"}:
            oauth: dict[str, Any] = {}
            if config.oauth_client_id:
                oauth["client_id"] = config.oauth_client_id
            if config.oauth_callback_port is not None:
                oauth["callback_port"] = config.oauth_callback_port
            if oauth:
                server_entry["oauth"] = oauth
    if config.startup_timeout_sec is not None:
        server_entry["startup_timeout_sec"] = config.startup_timeout_sec
    if config.tool_timeout_sec is not None:
        server_entry["tool_timeout_sec"] = config.tool_timeout_sec
    if config.required:
        server_entry["required"] = True
    if config.supports_parallel_tool_calls:
        server_entry["supports_parallel_tool_calls"] = True
    if config.enabled_tools is not None:
        server_entry["enabled_tools"] = list(config.enabled_tools)
    if config.disabled_tools:
        server_entry["disabled_tools"] = list(config.disabled_tools)
    if config.default_tools_approval_mode is not None:
        server_entry["default_tools_approval_mode"] = config.default_tools_approval_mode
    if config.tool_approval_modes:
        server_entry["tools"] = {
            name: {"approval_mode": mode}
            for name, mode in config.tool_approval_modes.items()
        }
    return server_entry


def _server_entry_from_payload(
    config: MCPServerConfig,
    data: dict[str, Any],
    *,
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    entry = dict(existing or {})
    canonical_entry = _server_entry_from_config(config)
    entry.update(canonical_entry)
    entry["auto_start"] = config.auto_start
    if config.transport == "stdio":
        entry.pop("url", None)
        entry.pop("headers", None)
        entry.pop("headers_helper", None)
        entry.pop("oauth", None)
    else:
        entry.pop("command", None)
        entry.pop("args", None)
        entry.pop("env", None)
        entry.pop("env_vars", None)
        entry.pop("cwd", None)
        if config.transport == "ws":
            entry.pop("oauth", None)
    if config.transport == "stdio" and not config.env:
        entry.pop("env", None)
    if config.transport != "stdio" and not config.headers:
        entry.pop("headers", None)
    if config.transport != "stdio" and not config.headers_helper:
        entry.pop("headers_helper", None)
    if config.transport in {"sse", "http"} and not config.oauth_client_id and config.oauth_callback_port is None:
        entry.pop("oauth", None)
    env_vars = data.get("env_vars")
    if config.transport == "stdio" and isinstance(env_vars, list) and env_vars:
        entry["env_vars"] = env_vars
    else:
        entry.pop("env_vars", None)
    for field in (
        "startup_timeout_sec",
        "tool_timeout_sec",
        "required",
        "supports_parallel_tool_calls",
        "enabled_tools",
        "disabled_tools",
        "default_tools_approval_mode",
        "tools",
    ):
        if field in data and field not in canonical_entry:
            entry.pop(field, None)
    return entry


async def _upsert_mcp_config(
    name: str,
    server_entry: dict[str, Any],
    *,
    config_change_hook: ConfigChangeHook,
) -> None:
    current_data = _read_current_config_data()
    servers_data = current_data.setdefault("servers", {})
    if not isinstance(servers_data, dict):
        raise MCPServiceError("MCP config must contain an object field named 'servers'.")
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
    hook_result = await config_change_hook(source="mcp", file_path=str(MCP_CONFIG_FILE))
    _assert_config_change_allowed(
        hook_result,
        source="mcp",
        file_path=str(MCP_CONFIG_FILE),
    )
    config_file_mod.write_mcp_config(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        MCP_CONFIG_FILE,
    )


def installed_connector_names(manager: Any | None) -> set[str]:
    installed_names: set[str] = set()
    config = config_file_mod.read_mcp_config()
    config_data = json.loads(str(config.get("content") or "{}"))
    servers = config_data.get("servers", {}) if isinstance(config_data, dict) else {}
    if isinstance(servers, dict):
        installed_names.update(str(name) for name in servers.keys())
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


def _optional_positive_timeout(value: Any, field: str) -> float | None:
    if value is None:
        return None
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or float(value) <= 0
    ):
        raise MCPServiceError(f"MCP {field} must be a positive number")
    return float(value)


def _optional_tool_names(value: Any, *, preserve_none: bool) -> list[str] | None:
    if value is None:
        return None if preserve_none else []
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise MCPServiceError("MCP tool filters must be arrays of non-empty strings")
    return list(dict.fromkeys(item.strip() for item in value))


def _optional_approval_mode(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value not in {"auto", "prompt", "writes", "approve"}:
        raise MCPServiceError(f"MCP {field} must be auto, prompt, writes, or approve")
    return value


def _tool_approval_modes(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise MCPServiceError("MCP tools must be an object")
    result: dict[str, str] = {}
    for tool_name, tool_config in value.items():
        if not isinstance(tool_name, str) or not tool_name.strip():
            raise MCPServiceError("MCP tools contains an invalid tool name")
        if not isinstance(tool_config, Mapping):
            raise MCPServiceError(f"MCP tools.{tool_name} must be an object")
        mode = _optional_approval_mode(
            tool_config.get("approval_mode"),
            f"tools.{tool_name}.approval_mode",
        )
        if mode is not None:
            result[tool_name] = mode
    return result


def _active_project_mcp_workspace(
    manager: Any,
    server_name: str,
    expected_workspace: str | Path,
) -> Path:
    clean_workspace = str(expected_workspace or "").strip()
    if not clean_workspace:
        raise MCPServiceError("Project MCP approval requires an explicit workspace owner")
    workspace_root = Path(clean_workspace).expanduser().resolve()
    status = next(
        (item for item in manager.get_all_status() if str(item.get("name") or "") == server_name),
        None,
    )
    if not status or status.get("source") != "project":
        raise MCPServiceError(f"MCP server '{server_name}' is not a project .mcp.json server")
    configured_workspace = str(status.get("project_workspace") or "")
    configured_root = (
        Path(configured_workspace).expanduser().resolve()
        if configured_workspace
        else None
    )
    if (
        configured_root is None
        or os.path.normcase(str(workspace_root))
        != os.path.normcase(str(configured_root))
    ):
        raise MCPServiceError("The project MCP catalog changed; refresh it before approving")
    return workspace_root


def _require_connectable_server(manager: Any, server_name: str) -> None:
    status = next(
        (item for item in manager.get_all_status() if str(item.get("name") or "") == server_name),
        None,
    )
    if status is None:
        raise MCPServiceError(f"MCP server '{server_name}' is not configured")
    if status.get("enabled") is False:
        reason = str(status.get("disabled_reason") or "managed MCP policy")
        raise MCPServiceError(f"MCP server '{server_name}' is disabled: {reason}")
    if status.get("source") == "project" and status.get("approval_status") != PROJECT_MCP_APPROVED:
        raise MCPServiceError(f"MCP server '{server_name}' has not been approved for this project")
