from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path


def default_plugin_roots() -> list[Path]:
    """Return user-installed plugin roots plus the legacy MiniCode root.

    ``~/.codex/plugins/cache`` is a marketplace/download cache, not Codex's
    enabled-plugin set. Scanning it made every cached connector active in
    MiniCode and could launch OAuth for apps the user never installed here.
    Plugins can still be imported explicitly into the shared ``~/.agents``
    root, matching the Codex plugin installation boundary.
    """
    roots: list[Path] = []
    explicit = os.environ.get("MINICODE_PLUGINS_DIR", "").strip()
    if explicit:
        roots.extend(Path(part).expanduser() for part in explicit.split(os.pathsep) if part.strip())

    roots.append(Path.home() / ".agents" / "plugins" / "plugins")
    roots.append(Path.home() / ".agents" / "plugins")
    # Read existing installations during migration; new imports use .agents/plugins.
    roots.append(Path(os.environ.get("MINICODE_HOME") or (Path.home() / ".minicode")).expanduser() / "plugins")

    seen: set[str] = set()
    unique: list[Path] = []
    for root in roots:
        key = str(root.resolve()) if root.exists() else str(root.absolute())
        if key in seen:
            continue
        seen.add(key)
        unique.append(root)
    return unique


def _iter_plugin_manifests(plugin_roots: Iterable[Path]) -> list[Path]:
    """Discover only the official Codex plugin manifest."""
    manifests: list[Path] = []
    seen: set[str] = set()
    for raw_root in plugin_roots:
        root = Path(raw_root).expanduser()
        candidates: list[Path] = []
        if root.is_file() and root.name == "plugin.json" and root.parent.name == ".codex-plugin":
            candidates.append(root)
        elif root.is_dir():
            if root.name == ".codex-plugin":
                candidates.append(root / "plugin.json")
            else:
                candidates.extend(root.glob("*/.codex-plugin/plugin.json"))
                candidates.extend(root.glob("cache/*/*/*/.codex-plugin/plugin.json"))
                candidates.append(root / ".codex-plugin" / "plugin.json")
        for candidate in candidates:
            if not candidate.is_file():
                continue
            key = str(candidate.resolve())
            if key in seen:
                continue
            seen.add(key)
            manifests.append(candidate)
    return sorted(manifests, key=lambda path: str(path).casefold())
