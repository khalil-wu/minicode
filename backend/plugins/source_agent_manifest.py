"""Agent Plugins v1 root manifests, as admitted by Codex's manifest cache."""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger(__name__)
AGENT_PLUGIN_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
AGENT_PLUGIN_SCHEMA_PREFIX = "https://agent-plugins.org/schemas/"
_FIELDS = {"$schema", "name", "version", "description", "author", "homepage", "repository", "license", "keywords", "extensions"}


def is_agent_plugin_schema(raw: Mapping[str, Any]) -> bool:
    schema = raw.get("$schema")
    return isinstance(schema, str) and schema.startswith(AGENT_PLUGIN_SCHEMA_PREFIX)


def resolve_agent_manifest(raw: Mapping[str, Any], root: Path) -> dict[str, Any]:
    if raw.get("$schema") != AGENT_PLUGIN_SCHEMA:
        raise ValueError(f"unsupported Agent Plugins schema {raw.get('$schema')!r}; expected {AGENT_PLUGIN_SCHEMA}")
    name = raw.get("name")
    if not isinstance(name, str) or len(name.encode("utf-8")) > 64 or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", name) or "--" in name or ".." in name:
        raise ValueError("invalid Agent Plugins name; use lowercase letters, numbers, dots, or hyphens")
    for field in ("version", "description", "homepage", "repository", "license"):
        if field in raw and not isinstance(raw[field], str):
            raise ValueError(f"Agent Plugins {field} must be a string when present")
    for field in set(raw) - _FIELDS:
        logger.warning("Ignoring unknown Agent Plugins manifest field %s", field)
    resolved = {key: raw[key] for key in ("name", "version", "description", "keywords") if key in raw}
    resolved.update(skills="./skills", mcp_servers="./mcp.json")
    author = raw.get("author", {})
    if not isinstance(author, Mapping) or any(not isinstance(value, str) for value in author.values()) or set(author) - {"name", "email", "url"}:
        raise ValueError("Agent Plugins author must contain only string name/email/url fields")
    resolved["interface"] = {"displayName": name, "shortDescription": raw.get("description", ""),
        "longDescription": raw.get("description", ""), "developerName": author.get("name", ""),
        "category": "Other", "websiteUrl": raw.get("homepage", "")}
    extensions = raw.get("extensions", {})
    extension = extensions.get("com.openai") if isinstance(extensions, Mapping) else None
    if isinstance(extension, Mapping):
        for field in ("apps", "hooks", "interface"):
            if field in extension:
                resolved[field] = extension[field]
    elif extension is not None:
        logger.warning("Ignoring non-object Agent Plugins com.openai extension")
    return resolved
