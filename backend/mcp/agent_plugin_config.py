"""Agent Plugins v1 MCP admission, following Codex's format-specific parser."""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

AGENT_PLUGIN_MCP_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"
_CLIENT_OWNED_HEADERS = frozenset({
    "accept", "authorization", "connection", "content-encoding", "content-length",
    "content-type", "host", "last-event-id", "mcp-protocol-version", "mcp-session-id",
    "proxy-authorization", "te", "trailer", "transfer-encoding", "upgrade", "user-agent",
})
_PLACEHOLDER = re.compile(r"\$\{PLUGIN_(ROOT|DATA)\}")
_HEADER_NAME = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
_ASCII_UPPER = str.maketrans("abcdefghijklmnopqrstuvwxyz", "ABCDEFGHIJKLMNOPQRSTUVWXYZ")


def ascii_upper(value: str) -> str:
    return value.translate(_ASCII_UPPER)


class _JsonObject(dict):
    def __init__(self, pairs: list[tuple[str, Any]]) -> None:
        super().__init__(pairs)
        self.duplicates = len(pairs) != len(self)


@dataclass
class PluginMcpConfigParseOutcome:
    servers: dict[str, dict[str, Any]] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)


def parse_agent_plugin_mcp_config(
    plugin_root: Path, plugin_data_root: Path, contents: str,
) -> PluginMcpConfigParseOutcome:
    payload = json.loads(contents, object_pairs_hook=_JsonObject)
    if not isinstance(payload, Mapping) or set(payload) != {"$schema", "mcpServers"}:
        raise ValueError("Agent Plugins MCP config must contain only $schema and mcpServers")
    if payload.duplicates:
        raise ValueError("duplicate Agent Plugins MCP config field")
    if payload["$schema"] != AGENT_PLUGIN_MCP_SCHEMA:
        raise ValueError(f"unsupported Agent Plugins MCP schema {payload['$schema']!r}")
    if not isinstance(payload["mcpServers"], Mapping):
        raise ValueError("Agent Plugins mcpServers must be an object")
    outcome = PluginMcpConfigParseOutcome()
    for name, value in sorted(payload["mcpServers"].items()):
        try:
            outcome.servers[name] = _normalize_server(value, plugin_root, plugin_data_root)
        except (ValueError, OSError) as exc:
            outcome.errors[name] = str(exc)
    return outcome


def load_agent_plugin_mcp_config(
    plugin_root: Path, plugin_data_root: Path,
) -> PluginMcpConfigParseOutcome:
    """Invalid v1 files disable this plugin's MCP, with diagnostic evidence."""
    config_path = plugin_root / "mcp.json"
    try:
        metadata = config_path.lstat()
    except FileNotFoundError:
        return PluginMcpConfigParseOutcome()
    except OSError as exc:
        logger.warning("Failed to inspect Agent Plugins MCP config %s: %s", config_path, exc)
        return PluginMcpConfigParseOutcome(errors={"<config>": str(exc)})
    try:
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("Agent Plugins MCP config must be a regular, non-symlink file")
        config_path.resolve(strict=True).relative_to(plugin_root.resolve(strict=True))
        outcome = parse_agent_plugin_mcp_config(
            plugin_root, plugin_data_root, config_path.read_text(encoding="utf-8"),
        )
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        logger.warning("Failed to parse Agent Plugins MCP config %s: %s", config_path, exc)
        return PluginMcpConfigParseOutcome(errors={"<config>": str(exc)})
    for name, message in outcome.errors.items():
        logger.warning("Failed to parse Agent Plugins MCP server %s in %s: %s", name, config_path, message)
    return outcome


def _normalize_server(value: Any, plugin_root: Path, plugin_data_root: Path) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("Agent Plugins MCP server must be an object")
    transport = value.get("type")
    if not isinstance(transport, str):
        raise ValueError("Agent Plugins MCP server type must be a string")
    if transport == "stdio":
        return _normalize_stdio(value, plugin_root, plugin_data_root)
    if transport in {"streamable-http", "sse"}:
        if set(value) - {"type", "url", "headers"}:
            raise ValueError("unsupported Agent Plugins HTTP server fields")
        url = _string(value, "url")
        headers = _string_map(value, "headers") if "headers" in value else {}
        if transport == "sse":
            raise ValueError("Agent Plugins legacy SSE transport is not supported by Codex")
        _validate_url(url)
        seen: set[str] = set()
        for name, content in headers.items():
            lowered = name.lower()
            if lowered in seen:
                raise ValueError(f"duplicate case-insensitive Agent Plugins HTTP header {name!r}")
            seen.add(lowered)
            if _HEADER_NAME.fullmatch(name) is None:
                raise ValueError(f"invalid Agent Plugins HTTP header name {name!r}")
            if any((ord(char) < 32 and char != "\t") or ord(char) == 127 for char in content):
                raise ValueError(f"invalid Agent Plugins HTTP header value for {name!r}")
        return {"transport": "http", "url": url,
            "headers": {name: content for name, content in headers.items() if name.lower() not in _CLIENT_OWNED_HEADERS}}
    raise ValueError(f"unsupported Agent Plugins MCP server type {transport!r}")


def _normalize_stdio(value: Mapping[str, Any], plugin_root: Path, plugin_data_root: Path) -> dict[str, Any]:
    if set(value) - {"type", "command", "args", "env", "cwd"}:
        raise ValueError("unsupported Agent Plugins stdio server fields")
    command = _string(value, "command")
    args = value.get("args", [])
    if not isinstance(args, list) or any(not isinstance(arg, str) for arg in args):
        raise ValueError("Agent Plugins stdio args must be an array of strings")
    env = _string_map(value, "env") if "env" in value else {}
    bare_command = bool(command) and "/" not in command and "\\" not in command and not Path(command).drive
    relative_command = command.startswith("./") and _portable_suffix(command[2:])
    if not bare_command and not relative_command:
        raise ValueError("Agent Plugins stdio command must be a bare executable name or a contained ./ path")
    names_match = ascii_upper if os.name == "nt" else (lambda name: name)
    for name in env:
        if names_match(name) in {"PLUGIN_ROOT", "PLUGIN_DATA"}:
            raise ValueError(f"Agent Plugins stdio env cannot override reserved variable {name!r}")
    if os.name == "nt":
        normalized: dict[str, str] = {}
        for name, content in env.items():
            upper = ascii_upper(name)
            if upper in normalized:
                raise ValueError(f"duplicate case-insensitive Agent Plugins environment variable {name!r}")
            normalized[upper] = content
        env = normalized
    root = _resolve_existing_path_prefix(plugin_root)
    data_root = _resolve_existing_path_prefix(plugin_data_root)
    replacements = {"ROOT": str(root), "DATA": str(data_root)}
    expand = lambda text: _PLACEHOLDER.sub(lambda match: replacements[match[1]], text)
    if relative_command:
        command = str(_contained_path(command, root))
    cwd = _string(value, "cwd") if "cwd" in value else "${PLUGIN_ROOT}"
    if cwd == "./" or cwd.startswith("./") and _portable_suffix(cwd[2:]):
        cwd_root = root
    elif cwd in {"${PLUGIN_ROOT}", "${PLUGIN_DATA}"}:
        cwd_root = root if cwd == "${PLUGIN_ROOT}" else data_root
    elif any(cwd.startswith(prefix + "/") and (not cwd[len(prefix) + 1:] or _portable_suffix(cwd[len(prefix) + 1:]))
            for prefix in ("${PLUGIN_ROOT}", "${PLUGIN_DATA}")):
        cwd_root = root if cwd.startswith("${PLUGIN_ROOT}/") else data_root
    else:
        raise ValueError("Agent Plugins stdio cwd must be a contained ./, ${PLUGIN_ROOT}, or ${PLUGIN_DATA} path")
    return {"transport": "stdio", "command": command, "args": [expand(arg) for arg in args],
        "env": {**{name: expand(content) for name, content in env.items()},
            "PLUGIN_ROOT": str(root), "PLUGIN_DATA": str(data_root)},
        "cwd": str(_contained_path(expand(cwd), cwd_root))}


def _portable_suffix(value: str) -> bool:
    return bool(value) and "\\" not in value


def _contained_path(value: str, root: Path) -> Path:
    path = _resolve_existing_path_prefix(root / value)
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"expanded path {value!r} must remain within {str(root)!r}") from exc
    return path


def _resolve_existing_path_prefix(path: Path) -> Path:
    existing = path.absolute()
    missing: list[str] = []
    while True:
        try:
            resolved = existing.resolve(strict=True)
            return Path(os.path.normpath(resolved.joinpath(*reversed(missing))))
        except FileNotFoundError:
            if existing.is_symlink():
                raise ValueError(f"failed to resolve symlinked path {str(path)!r}")
            if existing.parent == existing:
                raise
            missing.append(existing.name)
            existing = existing.parent


def _string(value: Mapping[str, Any], name: str) -> str:
    result = value.get(name)
    if not isinstance(result, str):
        raise ValueError(f"Agent Plugins MCP {name} must be a string")
    return result


def _string_map(value: Mapping[str, Any], name: str) -> dict[str, str]:
    result = value[name]
    if not isinstance(result, Mapping) or any(not isinstance(key, str) or not isinstance(item, str) for key, item in result.items()):
        raise ValueError(f"Agent Plugins MCP {name} must map strings to strings")
    return dict(result)


def _validate_url(value: str) -> None:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Agent Plugins MCP URL must be absolute HTTP or HTTPS")
    _ = parsed.port
    if parsed.username or parsed.password is not None or "#" in value:
        raise ValueError("Agent Plugins MCP URL must not contain user information or a fragment")
    host = parsed.hostname
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = _url_ipv4_address(host)
    loopback = address.is_loopback if address is not None else host == "localhost"
    if parsed.scheme == "http" and not loopback:
        raise ValueError("non-loopback Agent Plugins MCP endpoints must use HTTPS")


def _url_ipv4_address(host: str) -> ipaddress.IPv4Address | None:
    """WHATWG numeric hosts used by Rust url::Url, including 127.1 and hex."""
    parts = host.removesuffix(".").split(".")

    def number(part: str) -> int:
        base = 10
        if part.startswith(("0x", "0X")):
            base, part = 16, part[2:]
        elif part.startswith("0") and len(part) > 1:
            base, part = 8, part[1:]
        if not part:
            return 0
        alphabet = "0123456789abcdef"[:base]
        if any(char.lower() not in alphabet for char in part):
            raise ValueError("invalid Agent Plugins MCP IPv4 URL host")
        return int(part, base)

    last = parts[-1]
    try:
        number(last)
    except ValueError:
        if not last or not last.isascii() or not last.isdigit():
            return None
    if len(parts) > 4 or any(not part for part in parts):
        raise ValueError("invalid Agent Plugins MCP IPv4 URL host")
    numbers = [number(part) for part in parts]
    if any(value > 255 for value in numbers[:-1]) or numbers[-1] >= 256 ** (5 - len(numbers)):
        raise ValueError("invalid Agent Plugins MCP IPv4 URL host")
    value = numbers[-1] + sum(part * 256 ** (3 - index) for index, part in enumerate(numbers[:-1]))
    return ipaddress.IPv4Address(value)
