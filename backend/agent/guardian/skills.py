"""Source runtime-only, turn-matched invoked user-owned skill evidence."""

from __future__ import annotations

import json
from pathlib import Path

from backend.agent.markdown_scopes import get_minicode_config_home_dir


def verified_skill_path(resource: str) -> str | None:
    try:
        path = Path(resource).resolve(strict=True)
    except OSError:
        return None
    for root in (get_minicode_config_home_dir() / "skills", Path.home() / ".agents" / "skills"):
        try:
            canonical_root = root.resolve(strict=True)
        except OSError:
            continue
        if path.is_relative_to(canonical_root) and path.is_file() and len(str(path).encode("utf-8")) <= 512:
            return str(path)
    return None


def bounded_skill_paths(paths) -> tuple[str, ...]:
    selected = set()
    for path in paths:
        if path in selected or len(selected) >= 16 or sum(len(value.encode("utf-8")) for value in selected) + len(path.encode("utf-8")) > 2048:
            continue
        selected.add(path)
    return tuple(sorted(selected))


def render_trusted_skills(paths) -> str:
    prefix = "Codex-verified invoked user-owned skill paths:\n"
    encoded = "["
    for path in paths:
        separator = "" if encoded.endswith("[") else ","
        value = json.dumps(path, ensure_ascii=False)
        if len((encoded + separator + value + "]").encode("utf-8")) <= 768 * 4 - len(prefix.encode("utf-8")):
            encoded += separator + value
    return prefix + encoded + "]"
