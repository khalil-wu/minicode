from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.mcp.manager import MCP_CONFIG_FILE

DEFAULT_MCP_CONFIG = {"mcpServers": {}}


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
    data = _parse_and_validate_config(content)
    normalized = _format_config(data)
    config_path.parent.mkdir(parents=True, exist_ok=True)

    backup_path: Path | None = None
    if config_path.exists():
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        backup_path = config_path.with_name(f"{config_path.name}.{timestamp}.bak")
        shutil.copy2(config_path, backup_path)

    config_path.write_text(normalized, encoding="utf-8")
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

    servers = raw.get("mcpServers", {})
    if not isinstance(servers, dict):
        raise ValueError("MCP config must contain an object field named 'mcpServers'.")

    normalized_servers: dict[str, dict[str, Any]] = {}
    for name, server in servers.items():
        server_name = str(name).strip()
        if not server_name:
            raise ValueError("MCP server names cannot be empty.")
        if not isinstance(server, dict):
            raise ValueError(f"MCP server '{server_name}' must be an object.")
        normalized_servers[server_name] = _validate_server(server_name, server)

    result = dict(raw)
    result["mcpServers"] = normalized_servers
    return result


def _validate_server(name: str, server: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(server)
    transport = str(normalized.get("transport") or "").strip().lower()
    if not transport:
        transport = "http" if str(normalized.get("url") or "").strip() else "stdio"
    if transport not in {"stdio", "http"}:
        raise ValueError(f"MCP server '{name}' transport must be 'stdio' or 'http'.")
    normalized["transport"] = transport

    command = normalized.get("command", "python" if transport == "stdio" else None)
    if command is not None and not isinstance(command, str):
        raise ValueError(f"MCP server '{name}' command must be a string.")
    if transport == "stdio" and not str(command or "").strip():
        raise ValueError(f"MCP server '{name}' requires a command for stdio transport.")
    if command is not None and str(command).strip():
        normalized["command"] = command
    elif "command" in normalized:
        normalized.pop("command", None)

    args = normalized.get("args", [])
    if not isinstance(args, list) or any(not isinstance(item, str) for item in args):
        raise ValueError(f"MCP server '{name}' args must be an array of strings.")
    normalized["args"] = args

    env = normalized.get("env", {})
    if not isinstance(env, dict) or any(not isinstance(key, str) or not isinstance(value, str) for key, value in env.items()):
        raise ValueError(f"MCP server '{name}' env must be an object of string values.")
    normalized["env"] = env

    url = normalized.get("url")
    if url is not None:
        normalized["url"] = str(url).strip()
    if transport == "http" and not str(normalized.get("url") or "").strip():
        raise ValueError(f"MCP server '{name}' requires a url for http transport.")

    if "autoStart" in normalized and not isinstance(normalized["autoStart"], bool):
        raise ValueError(f"MCP server '{name}' autoStart must be a boolean.")

    if "maxRetries" in normalized:
        try:
            max_retries = int(normalized["maxRetries"])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"MCP server '{name}' maxRetries must be an integer.") from exc
        if max_retries < 0 or max_retries > 20:
            raise ValueError(f"MCP server '{name}' maxRetries must be between 0 and 20.")
        normalized["maxRetries"] = max_retries

    if "requiresUserAction" in normalized and not isinstance(normalized["requiresUserAction"], bool):
        raise ValueError(f"MCP server '{name}' requiresUserAction must be a boolean.")

    for field in ("setupHint", "docsUrl"):
        if field in normalized and not isinstance(normalized[field], str):
            raise ValueError(f"MCP server '{name}' {field} must be a string.")

    return normalized


def _summarize_servers(data: dict[str, Any]) -> list[dict[str, Any]]:
    servers = data.get("mcpServers", {})
    if not isinstance(servers, dict):
        return []
    return [
        {
            "name": name,
            "transport": server.get("transport", "stdio") if isinstance(server, dict) else "stdio",
            "autoStart": server.get("autoStart", True) if isinstance(server, dict) else True,
            "hasEnv": bool(server.get("env")) if isinstance(server, dict) else False,
            "hasUrl": bool(server.get("url")) if isinstance(server, dict) else False,
        }
        for name, server in servers.items()
    ]


def _format_config(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"
