from __future__ import annotations

import json
import os
from pathlib import Path

from backend.config import DATA_ROOT
from backend.atomic_io import atomic_write_text, file_mutation_locks


TRUSTED_WORKSPACES_FILE = Path(DATA_ROOT) / "trusted_workspaces.json"


def trust_created_task_directory(task_directory: Path) -> None:
    """Register the actual directory minted by a projectless create operation."""
    directory = task_directory.resolve()
    with file_mutation_locks([TRUSTED_WORKSPACES_FILE]):
        payload = json.loads(TRUSTED_WORKSPACES_FILE.read_text(encoding="utf-8")) if TRUSTED_WORKSPACES_FILE.exists() else []
        roots = payload if isinstance(payload, list) else payload["roots"]
        if any(directory.is_relative_to(Path(root).expanduser().resolve()) for root in roots):
            return
        roots.append(str(directory))
        TRUSTED_WORKSPACES_FILE.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(TRUSTED_WORKSPACES_FILE, json.dumps(payload, ensure_ascii=False, indent=2))


def _trusted_workspace_values() -> list[str]:
    try:
        payload = json.loads(TRUSTED_WORKSPACES_FILE.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return []
    values = (
        payload
        if isinstance(payload, list)
        else payload.get("roots", [])
        if isinstance(payload, dict)
        else []
    )
    if not isinstance(values, list):
        return []
    return [value for value in values if isinstance(value, str) and value.strip()]


def _resolved_trusted_roots(values):
    seen: set[str] = set()
    for value in values:
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
        yield candidate


def trusted_workspace_roots() -> tuple[Path, ...]:
    """Read the desktop main process' authoritative workspace-trust ledger."""
    return tuple(_resolved_trusted_roots(_trusted_workspace_values()))


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
    # Native picker/task creation stores canonical roots. Check lexical
    # ancestors first, then resolve their actual current targets before trust.
    # Alias entries still participate; no authorization survives a ledger edit
    # or a directory/junction change through a retained result cache.
    values = _trusted_workspace_values()
    values.sort(key=lambda value: not resolved.is_relative_to(Path(value).expanduser().absolute()))
    for trusted_root in _resolved_trusted_roots(values):
        try:
            resolved.relative_to(trusted_root)
            return True
        except ValueError:
            continue
    return False
