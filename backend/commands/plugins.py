from __future__ import annotations

import os
import json
import re
from collections.abc import Iterable
from pathlib import Path

from backend.atomic_io import canonical_file_path_key
from backend.config import STATE_ROOT
from backend.plugins.layout import PLUGIN_MANIFEST_DIRECTORY, plugin_manifest_path


def default_plugin_roots() -> list[Path]:
    """Return only MiniCode-owned plugin roots."""
    roots: list[Path] = []
    explicit = os.environ.get("MINICODE_PLUGINS_DIR", "").strip()
    if explicit:
        roots.extend(Path(part).expanduser() for part in explicit.split(os.pathsep) if part.strip())

    roots.append(STATE_ROOT / "extensions" / "plugins" / "installed")
    roots.append(STATE_ROOT / "extensions" / "plugins" / "store")
    roots.append(STATE_ROOT / "extensions" / "plugins")
    seen: set[str] = set()
    unique: list[Path] = []
    for root in roots:
        key = canonical_file_path_key(root)
        if key in seen:
            continue
        seen.add(key)
        unique.append(root)
    return unique


def _iter_plugin_manifests(plugin_roots: Iterable[Path]) -> list[Path]:
    """Discover active MiniCode plugin manifests deterministically.

    Marketplace materializations use ``<marketplace>/<plugin>/<version>`` with
    an ``active.json`` selector.
    Historical versions must never become simultaneously executable, so this
    function selects the explicit active version (or the highest valid version
    only for stores migrating without a selector).
    """
    manifests: list[Path] = []
    seen: set[str] = set()
    for raw_root in plugin_roots:
        root = Path(raw_root).expanduser()
        candidates: list[Path] = []
        if root.is_file() and root.name == "plugin.json" and root.parent.name == PLUGIN_MANIFEST_DIRECTORY:
            candidates.append(root)
        elif root.is_dir():
            if root.name == PLUGIN_MANIFEST_DIRECTORY:
                candidates.append(root / "plugin.json")
            else:
                candidates.extend(root.glob(f"*/{PLUGIN_MANIFEST_DIRECTORY}/plugin.json"))
                candidates.append(plugin_manifest_path(root))
                candidates.extend(_active_versioned_manifests(root))
                candidates.extend(root.glob(f"cache/*/*/*/{PLUGIN_MANIFEST_DIRECTORY}/plugin.json"))
        for candidate in candidates:
            if not candidate.is_file():
                continue
            key = canonical_file_path_key(candidate)
            if key in seen:
                continue
            seen.add(key)
            manifests.append(candidate)
    return sorted(manifests, key=lambda path: str(path).casefold())


def _active_versioned_manifests(root: Path) -> list[Path]:
    # The canonical store validates metadata, selectors and path provenance
    # before exposing an active record.  Do not bypass that boundary by
    # globbing a forged ``<market>/<plugin>/<version>`` directory directly.
    if root.name.casefold() == "store" or any(
        path.is_file() for path in root.glob("*/*/*/.plugin-store.json")
    ):
        try:
            from backend.plugins.store import PluginStore

            records = PluginStore(root).list()
        except Exception:
            return []
        result: list[Path] = []
        for record in records:
            if not record.active:
                continue
            manifest = plugin_manifest_path(record.path)
            if manifest.is_file():
                result.append(manifest)
        return result
    result: list[Path] = []
    # root/marketplace/plugin/version/.minicode-plugin/plugin.json
    for marketplace in root.iterdir() if root.is_dir() else ():
        if not marketplace.is_dir() or marketplace.name.startswith(".") or marketplace.name == "cache":
            continue
        for plugin in marketplace.iterdir():
            if not plugin.is_dir() or plugin.name.startswith("."):
                continue
            versions = [item for item in plugin.iterdir() if item.is_dir() and _is_version_segment(item.name)]
            if not versions:
                continue
            selected = _read_active_version(plugin)
            if selected is None:
                selected = max(versions, key=lambda path: _version_key(path.name))
            else:
                selected = plugin / selected
            manifest = plugin_manifest_path(selected)
            if manifest.is_file():
                result.append(manifest)
    return result


def _read_active_version(plugin_dir: Path) -> str | None:
    try:
        payload = json.loads((plugin_dir / "active.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    version = payload.get("version") if isinstance(payload, dict) else None
    return str(version).strip() if isinstance(version, str) and version.strip() else None


def _is_version_segment(value: str) -> bool:
    return value == "local" or bool(re.fullmatch(r"v?\d+(?:\.\d+){1,3}(?:[-+][0-9A-Za-z.-]+)?", value))


def _version_key(value: str) -> tuple[object, ...]:
    if value == "local":
        return (1, 2**31, 0, 0, 0, 1, "")
    match = re.fullmatch(
        r"v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:\.(\d+))?(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?",
        value,
    )
    if not match:
        return (0, 0, 0, 0, 0, 0, value.casefold())
    numeric = tuple(int(part or 0) for part in match.groups()[:4])
    prerelease = str(match.group(5) or "")
    return (1, *numeric, 1 if not prerelease else 0, prerelease.casefold())
