"""Read the Codex stdio env_vars overlay for existing Agent Plugins servers."""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def load_agent_plugin_env_overlay(
    plugin_root: Path,
    normalize: Callable[[str, Mapping[str, Any]], tuple[tuple[str, str], ...]],
) -> dict[str, list[str]]:
    overlay_path = plugin_root / ".codex-plugin" / "plugin.json"
    try:
        manifest = json.loads(overlay_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        logger.warning("Failed to parse Codex Agent Plugin MCP overlay %s: %s", overlay_path, exc)
        return {}
    try:
        _validate_overlay_manifest(manifest)
        declaration = manifest.get("mcpServers")
        if isinstance(declaration, Mapping):
            servers = declaration.get("mcpServers", declaration)
        else:
            path = plugin_root / ".mcp.json"
            if declaration is not None:
                try:
                    if not isinstance(declaration, str) or not declaration.startswith("./"):
                        raise ValueError("Codex overlay MCP path must use ./ syntax")
                    relative = declaration[2:]
                    parts = re.split(r"[/\\]" if os.name == "nt" else "/", relative)
                    windows_root = os.name == "nt" and (relative.startswith("\\") or re.match(r"^[A-Za-z]:", relative))
                    if not relative or ".." in parts or relative.startswith("/") or windows_root:
                        raise ValueError("Codex overlay MCP path must be nonempty and contained without parent components")
                    path = plugin_root / relative
                except ValueError as exc:
                    logger.warning("Ignoring invalid Codex overlay mcpServers declaration: %s", exc)
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return {}
            if not isinstance(payload, Mapping):
                raise ValueError("Codex overlay MCP config must be an object")
            servers = payload.get("mcpServers", payload)
        if not isinstance(servers, Mapping):
            raise ValueError("Codex overlay MCP server declarations must be an object")
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        logger.warning("Failed to load Codex Agent Plugin MCP overlay %s: %s", overlay_path, exc)
        return {}
    result: dict[str, list[str]] = {}
    for name, server in servers.items():
        try:
            if not isinstance(server, Mapping):
                raise ValueError("Codex overlay MCP server must be an object")
            config = dict(server)
            config.pop("type", None)
            if "command" not in config:
                continue
            variables = config.get("env_vars", [])
            if not isinstance(variables, list):
                raise ValueError("Codex overlay env_vars must be an array")
            local_variables = []
            for variable in variables:
                if isinstance(variable, str):
                    local_variables.append(variable)
                elif (isinstance(variable, Mapping) and not set(variable) - {"name", "source"}
                    and isinstance(variable.get("name"), str)
                    and (variable.get("source") is None or isinstance(variable.get("source"), str)
                        and variable["source"] in {"local", "remote"})):
                    if variable.get("source") != "remote":
                        local_variables.append(variable["name"])
                else:
                    raise ValueError("Codex overlay env_vars entries must be names with local/remote sources")
            config["env_vars"] = local_variables
            config.setdefault("transport", "stdio")
            result[name] = [variable for variable, source in normalize(name, config) if source == "local"]
        except ValueError as exc:
            logger.warning("Failed to parse Codex Agent Plugin overlay server %s: %s", name, exc)
    return result


def _validate_overlay_manifest(value: Any) -> None:
    """The legacy RawPluginManifest typed fields are still parsed for overlays."""
    if not isinstance(value, Mapping):
        raise ValueError("Codex Agent Plugin overlay must be an object")
    if "name" in value and not isinstance(value["name"], str):
        raise ValueError("Codex overlay name must be a string")
    for field in ("version", "description", "apps"):
        if value.get(field) is not None and not isinstance(value[field], str):
            raise ValueError(f"Codex overlay {field} must be a string")
    if "keywords" in value and (not isinstance(value["keywords"], list)
        or any(not isinstance(item, str) for item in value["keywords"])):
        raise ValueError("Codex overlay keywords must be an array of strings")
    interface = value.get("interface")
    if interface is None:
        return
    if not isinstance(interface, Mapping):
        raise ValueError("Codex overlay interface must be an object")
    for field in ("displayName", "shortDescription", "longDescription", "developerName", "category",
        "websiteUrl", "websiteURL", "privacyPolicyUrl", "privacyPolicyURL", "termsOfServiceUrl", "termsOfServiceURL",
        "brandColor", "composerIcon", "logo", "logoDark"):
        if interface.get(field) is not None and not isinstance(interface[field], str):
            raise ValueError(f"Codex overlay interface.{field} must be a string")
    for field in ("capabilities", "screenshots"):
        if field in interface and (not isinstance(interface[field], list)
            or any(not isinstance(item, str) for item in interface[field])):
            raise ValueError(f"Codex overlay interface.{field} must be an array of strings")
