from __future__ import annotations

import json
import math
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.mcp.manager import MCP_CONFIG_FILE
from backend.mcp.transport import mcp_transport_from_mapping
from backend.atomic_io import atomic_write_text, file_mutation_locks

DEFAULT_MCP_CONFIG = {"servers": {}}
_MCP_CONFIG_WRITE_LOCK = threading.RLock()
_SERVER_FIELDS = frozenset({
    "transport", "command", "args", "env", "env_vars", "cwd", "url",
    "headers", "headers_helper", "oauth", "auto_start", "startup_timeout_sec",
    "tool_timeout_sec", "required", "supports_parallel_tool_calls",
    "enabled_tools", "disabled_tools", "default_tools_approval_mode", "tools",
    "requires_user_action", "setup_hint", "docs_url",
})


def read_mcp_config(config_path: Path = MCP_CONFIG_FILE) -> dict[str, Any]:
    """Read the local MCP config file without resolving secrets."""
    if not config_path.exists():
        content = _format_config(DEFAULT_MCP_CONFIG)
        return {
            "exists": False,
            "path": str(config_path),
            "content": content,
            "servers": [],
        }

    content = config_path.read_text(encoding="utf-8")
    data = _parse_and_validate_config(content)
    return {
        "exists": True,
        "path": str(config_path),
        "content": _format_config(data),
        "servers": _summarize_servers(data),
    }


def write_mcp_config(content: str, config_path: Path = MCP_CONFIG_FILE) -> dict[str, Any]:
    """Validate and persist .mcp.json, creating a timestamped backup first."""
    with _MCP_CONFIG_WRITE_LOCK:
        with file_mutation_locks([config_path]):
            data = _parse_and_validate_config(content)
            normalized = _format_config(data)
            config_path.parent.mkdir(parents=True, exist_ok=True)

            backup_path: Path | None = None
            if config_path.exists():
                timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
                backup_path = config_path.with_name(f"{config_path.name}.{timestamp}.bak")
                shutil.copy2(config_path, backup_path)

            atomic_write_text(config_path, normalized)
    return {
        "saved": True,
        "backup_path": str(backup_path) if backup_path else None,
        "config": read_mcp_config(config_path),
        "servers": _summarize_servers(data),
    }


def _parse_and_validate_config(content: str) -> dict[str, Any]:
    try:
        raw = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON: {exc.msg} at line {exc.lineno}, column {exc.colno}") from exc

    if not isinstance(raw, dict):
        raise ValueError("MCP config must be a JSON object.")

    unknown_root_fields = sorted(set(raw) - {"servers"})
    if unknown_root_fields:
        raise ValueError(
            "MCP config contains unsupported fields: " + ", ".join(unknown_root_fields)
        )
    servers = raw.get("servers", {})
    if not isinstance(servers, dict):
        raise ValueError("MCP config must contain an object field named 'servers'.")

    normalized_servers: dict[str, dict[str, Any]] = {}
    for name, server in servers.items():
        server_name = str(name).strip()
        if not server_name:
            raise ValueError("MCP server names cannot be empty.")
        if "\x00" in server_name:
            raise ValueError(f"Invalid MCP server name '{server_name}': names cannot contain NUL.")
        if not isinstance(server, dict):
            raise ValueError(f"MCP server '{server_name}' must be an object.")
        normalized_servers[server_name] = _validate_server(server_name, server)

    result = dict(raw)
    result["servers"] = normalized_servers
    return result


def _validate_server(name: str, server: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(server)
    unknown_fields = sorted(set(normalized) - _SERVER_FIELDS)
    if unknown_fields:
        raise ValueError(
            f"MCP server '{name}' contains unsupported fields: {', '.join(unknown_fields)}."
        )
    try:
        transport = mcp_transport_from_mapping(normalized)
    except ValueError as exc:
        raise ValueError(f"MCP server '{name}' has an invalid transport: {exc}.") from exc
    normalized["transport"] = transport

    stdio_only = ("command", "args", "env", "env_vars", "cwd")
    remote_only = ("url", "headers", "headers_helper")
    if transport == "stdio":
        _reject_nonempty_fields(name, transport, normalized, (*remote_only, "oauth"))
        for field in (*remote_only, "oauth"):
            normalized.pop(field, None)
    else:
        _reject_nonempty_fields(name, transport, normalized, stdio_only)
        for field in stdio_only:
            normalized.pop(field, None)
        if transport == "ws":
            _reject_nonempty_fields(name, transport, normalized, ("oauth",))
            normalized.pop("oauth", None)

    command = normalized.get("command")
    if command is not None and not isinstance(command, str):
        raise ValueError(f"MCP server '{name}' command must be a string.")
    if transport == "stdio" and not str(command or "").strip():
        raise ValueError(f"MCP server '{name}' requires a command for stdio transport.")
    if isinstance(command, str) and "\x00" in command:
        raise ValueError(f"MCP server '{name}' command cannot contain NUL.")
    if command is not None and str(command).strip():
        normalized["command"] = command
    elif "command" in normalized:
        normalized.pop("command", None)

    if transport == "stdio":
        args = normalized.get("args", [])
        if not isinstance(args, list) or any(not isinstance(item, str) for item in args):
            raise ValueError(f"MCP server '{name}' args must be an array of strings.")
        if any("\x00" in item for item in args):
            raise ValueError(f"MCP server '{name}' args cannot contain NUL.")
        normalized["args"] = args

        env = normalized.get("env", {})
        if not isinstance(env, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in env.items()
        ):
            raise ValueError(f"MCP server '{name}' env must be an object of string values.")
        if any("\x00" in key or "\x00" in value for key, value in env.items()):
            raise ValueError(f"MCP server '{name}' env cannot contain NUL.")
        normalized["env"] = env

        env_vars = normalized.get("env_vars", [])
        if not isinstance(env_vars, list) or any(
            not isinstance(item, (str, dict)) for item in env_vars
        ):
            raise ValueError(f"MCP server '{name}' env_vars must be an array of names or mappings.")
        for item in env_vars:
            if isinstance(item, str):
                if not item.strip() or "\x00" in item:
                    raise ValueError(f"MCP server '{name}' env_vars contains an invalid name.")
                continue
            var_name = item.get("name")
            source = item.get("source", var_name)
            unknown_fields = sorted(set(item) - {"name", "source"})
            if unknown_fields:
                raise ValueError(
                    f"MCP server '{name}' env_vars mapping contains unsupported fields: "
                    + ", ".join(str(field) for field in unknown_fields)
                )
            if not isinstance(var_name, str) or not var_name.strip() or "\x00" in var_name:
                raise ValueError(f"MCP server '{name}' env_vars mapping requires a valid name.")
            if not isinstance(source, str) or not source.strip() or "\x00" in source:
                raise ValueError(f"MCP server '{name}' env_vars mapping requires a valid source.")
        if env_vars:
            normalized["env_vars"] = env_vars
        else:
            normalized.pop("env_vars", None)

    headers = normalized.get("headers", {})
    if not isinstance(headers, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in headers.items()
    ):
        raise ValueError(f"MCP server '{name}' headers must be an object of string values.")
    if any("\x00" in key or "\x00" in value for key, value in headers.items()):
        raise ValueError(f"MCP server '{name}' headers cannot contain NUL.")
    if headers:
        normalized["headers"] = headers
    else:
        normalized.pop("headers", None)
    headers_helper = normalized.get("headers_helper", "")
    if not isinstance(headers_helper, str):
        raise ValueError(f"MCP server '{name}' headers_helper must be a string.")
    if "\x00" in headers_helper:
        raise ValueError(f"MCP server '{name}' headers_helper cannot contain NUL.")
    if headers_helper.strip():
        normalized["headers_helper"] = headers_helper.strip()
    else:
        normalized.pop("headers_helper", None)
    oauth = normalized.get("oauth", {}) if transport in {"sse", "http"} else {}
    if oauth is None:
        oauth = {}
    if not isinstance(oauth, dict):
        raise ValueError(f"MCP server '{name}' oauth must be an object.")
    unknown_oauth_fields = sorted(set(oauth) - {"client_id", "callback_port"})
    if unknown_oauth_fields:
        raise ValueError(
            f"MCP server '{name}' oauth contains unsupported fields: "
            + ", ".join(unknown_oauth_fields)
        )
    oauth_client_id = oauth.get("client_id", "")
    if not isinstance(oauth_client_id, str):
        raise ValueError(f"MCP server '{name}' oauth.client_id must be a string.")
    oauth_callback_port = oauth.get("callback_port")
    if oauth_callback_port is not None and (
        not isinstance(oauth_callback_port, int)
        or isinstance(oauth_callback_port, bool)
        or not (1 <= oauth_callback_port <= 65535)
    ):
        raise ValueError(f"MCP server '{name}' oauth.callback_port must be an integer between 1 and 65535.")
    normalized_oauth: dict[str, Any] = {}
    if oauth_client_id.strip():
        normalized_oauth["client_id"] = oauth_client_id.strip()
    if oauth_callback_port is not None:
        normalized_oauth["callback_port"] = oauth_callback_port
    if normalized_oauth:
        normalized["oauth"] = normalized_oauth
    else:
        normalized.pop("oauth", None)

    if transport == "stdio":
        cwd = normalized.get("cwd")
        if cwd is not None and not isinstance(cwd, str):
            raise ValueError(f"MCP server '{name}' cwd must be a string.")
        if isinstance(cwd, str) and "\x00" in cwd:
            raise ValueError(f"MCP server '{name}' cwd cannot contain NUL.")
        if isinstance(cwd, str) and not cwd.strip():
            normalized.pop("cwd", None)

    url = normalized.get("url")
    if url is not None and not isinstance(url, str):
        raise ValueError(f"MCP server '{name}' url must be a string.")
    if isinstance(url, str):
        normalized["url"] = url.strip()
    if transport in {"sse", "http", "ws"} and not str(normalized.get("url") or "").strip():
        raise ValueError(f"MCP server '{name}' requires a url for {transport} transport.")

    if "auto_start" in normalized and not isinstance(normalized["auto_start"], bool):
        raise ValueError(f"MCP server '{name}' auto_start must be a boolean.")

    for field in ("startup_timeout_sec", "tool_timeout_sec"):
        if field not in normalized:
            continue
        value = normalized[field]
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(float(value))
            or float(value) <= 0
        ):
            raise ValueError(f"MCP server '{name}' {field} must be a positive number.")
        normalized[field] = float(value)

    for field in ("required", "supports_parallel_tool_calls"):
        if field in normalized and not isinstance(normalized[field], bool):
            raise ValueError(f"MCP server '{name}' {field} must be a boolean.")
    for field in ("enabled_tools", "disabled_tools"):
        if field not in normalized:
            continue
        values = normalized[field]
        if not isinstance(values, list) or any(
            not isinstance(value, str) or not value.strip() for value in values
        ):
            raise ValueError(
                f"MCP server '{name}' {field} must be an array of non-empty strings."
            )
        normalized[field] = list(dict.fromkeys(value.strip() for value in values))
    approval_modes = {"auto", "prompt", "writes", "approve"}
    default_approval = normalized.get("default_tools_approval_mode")
    if default_approval is not None and default_approval not in approval_modes:
        raise ValueError(
            f"MCP server '{name}' default_tools_approval_mode must be one of: "
            "approve, auto, prompt, writes."
        )
    tools = normalized.get("tools")
    if tools is not None:
        if not isinstance(tools, dict):
            raise ValueError(f"MCP server '{name}' tools must be an object.")
        for tool_name, tool_config in tools.items():
            if not isinstance(tool_name, str) or not tool_name.strip():
                raise ValueError(f"MCP server '{name}' tools contains an invalid name.")
            if not isinstance(tool_config, dict):
                raise ValueError(f"MCP server '{name}' tools.{tool_name} must be an object.")
            unknown_tool_fields = sorted(set(tool_config) - {"approval_mode"})
            if unknown_tool_fields:
                raise ValueError(
                    f"MCP server '{name}' tools.{tool_name} contains unsupported fields: "
                    + ", ".join(unknown_tool_fields)
                )
            mode = tool_config.get("approval_mode")
            if mode is not None and mode not in approval_modes:
                raise ValueError(
                    f"MCP server '{name}' tools.{tool_name}.approval_mode is invalid."
                )

    if "requires_user_action" in normalized and not isinstance(normalized["requires_user_action"], bool):
        raise ValueError(f"MCP server '{name}' requires_user_action must be a boolean.")

    for field in ("setup_hint", "docs_url"):
        if field in normalized and not isinstance(normalized[field], str):
            raise ValueError(f"MCP server '{name}' {field} must be a string.")

    return normalized


def _has_nonempty_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict, set, frozenset)):
        return bool(value)
    return True


def _reject_nonempty_fields(
    name: str,
    transport: str,
    server: dict[str, Any],
    fields: tuple[str, ...],
) -> None:
    incompatible = [field for field in fields if _has_nonempty_value(server.get(field))]
    if incompatible:
        joined = ", ".join(incompatible)
        raise ValueError(
            f"MCP server '{name}' fields {joined} are not supported for {transport} transport."
        )


def _summarize_servers(data: dict[str, Any]) -> list[dict[str, Any]]:
    servers = data.get("servers", {})
    if not isinstance(servers, dict):
        raise ValueError("MCP config must contain an object field named 'servers'.")
    return [
        {
            "name": name,
            "transport": server["transport"],
            "auto_start": server.get("auto_start", True),
            "has_env": bool(server.get("env")),
            "has_url": bool(server.get("url")),
        }
        for name, server in servers.items()
    ]


def _format_config(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"
