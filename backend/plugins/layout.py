"""Canonical filesystem layout for MiniCode plugin packages."""

from __future__ import annotations

from pathlib import Path
import hashlib
import json


PLUGIN_MANIFEST_DIRECTORY = ".minicode-plugin"
PLUGIN_MANIFEST_FILENAME = "plugin.json"
MARKETPLACE_MANIFEST_FILENAME = "marketplace.json"


def plugin_manifest_path(plugin_root: Path) -> Path:
    from backend.plugins.source_agent_manifest import is_agent_plugin_schema

    root_manifest = plugin_root / "plugin.json"
    if root_manifest.is_file():
        try:
            raw = json.loads(root_manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            raw = None
        if isinstance(raw, dict) and is_agent_plugin_schema(raw):
            return root_manifest
    return plugin_root / PLUGIN_MANIFEST_DIRECTORY / PLUGIN_MANIFEST_FILENAME


def plugin_install_root() -> Path:
    from backend.config import STATE_ROOT

    return STATE_ROOT / "extensions" / "plugins" / "installed"


def _safe_plugin_data_segment(plugin_identity: str) -> str:
    from backend.plugins.identity import parse_plugin_id

    parsed = parse_plugin_id(plugin_identity)
    name = parsed.name or "plugin"
    marketplace = parsed.marketplace or "local"
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in f"{name}-{marketplace}")


def plugin_data_root(plugin_identity: str) -> Path:
    return plugin_install_root().parent / "data" / _safe_plugin_data_segment(plugin_identity)


def agent_plugin_data_root(plugin_identity: str) -> Path:
    from backend.plugins.identity import parse_plugin_id_strict

    parsed = parse_plugin_id_strict(plugin_identity)
    digest = hashlib.sha256(parsed.marketplace.encode("utf-8") + b"\0" + parsed.name.encode("utf-8")).hexdigest()
    return plugin_install_root().parent / "data" / "agent-plugins" / digest
