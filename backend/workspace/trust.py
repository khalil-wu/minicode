from __future__ import annotations

import json
import os
from pathlib import Path

from backend.config import DATA_ROOT


TRUSTED_WORKSPACES_FILE = Path(DATA_ROOT) / "trusted_workspaces.json"


def trusted_workspace_roots() -> tuple[Path, ...]:
    """Read the desktop main process' authoritative workspace-trust ledger."""

    try:
        payload = json.loads(TRUSTED_WORKSPACES_FILE.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return ()
    values = (
        payload
        if isinstance(payload, list)
        else payload.get("roots", [])
        if isinstance(payload, dict)
        else []
    )
    if not isinstance(values, list):
        return ()

    trusted: list[Path] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value.strip():
            continue
        try:
            candidate = Path(value).expanduser().resolve()
        except OSError:
            continue
        if not candidate.is_dir():
            continue
        key = os.path.normcase(str(candidate))
        if key in seen:
            continue
        seen.add(key)
        trusted.append(candidate)
    return tuple(trusted)


def is_workspace_trusted(workspace_root: Path | None) -> bool:
    """Match the desktop trust boundary: the chosen root and its descendants."""

    if workspace_root is None:
        return False
    try:
        resolved = Path(workspace_root).expanduser().resolve()
    except OSError:
        return False
    if not resolved.is_dir():
        return False
    for trusted_root in trusted_workspace_roots():
        try:
            resolved.relative_to(trusted_root)
            return True
        except ValueError:
            continue
    return False
