"""Canonical filesystem layout for MiniCode plugin packages."""

from __future__ import annotations

from pathlib import Path
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
