"""Canonical filesystem layout for MiniCode plugin packages."""

from __future__ import annotations

from pathlib import Path


PLUGIN_MANIFEST_DIRECTORY = ".minicode-plugin"
PLUGIN_MANIFEST_FILENAME = "plugin.json"
MARKETPLACE_MANIFEST_FILENAME = "marketplace.json"


def plugin_manifest_path(plugin_root: Path) -> Path:
    return plugin_root / PLUGIN_MANIFEST_DIRECTORY / PLUGIN_MANIFEST_FILENAME


def plugin_install_root() -> Path:
    from backend.config import STATE_ROOT

    return STATE_ROOT / "extensions" / "plugins" / "installed"
