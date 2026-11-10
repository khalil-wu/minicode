from __future__ import annotations

import json
import os
import stat
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


def _worktree_trust_root(directory: Path) -> Path | None:
    """Source git-utils trust: prove gitdir, commondir and checkout backlinks."""
    def metadata_text(path: Path) -> str:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > 64 * 1024:
            raise ValueError("Git metadata must be a bounded regular file")
        data = path.read_bytes()
        if len(data) > 64 * 1024:
            raise ValueError("Git metadata exceeds the Source size boundary")
        return os.fsdecode(data.strip())

    def gitdir(path: Path) -> Path:
        value = metadata_text(path)
        target = value[7:].strip(" \t\r\n\v\f")
        if not value.startswith("gitdir:") or not target:
            raise ValueError("Missing Git directory pointer")
        return Path(os.path.abspath(path.parent / target))

    try:
        checkout = directory
        while not os.path.lexists(checkout / ".git"):
            if checkout.parent == checkout:
                return None
            checkout = checkout.parent
        dot_git = checkout / ".git"
        if not dot_git.is_file():
            return None
        linked = gitdir(dot_git)
        info = linked.lstat()
        if not stat.S_ISDIR(info.st_mode) or os.name == "nt" and info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            return None
        linked = linked.resolve(strict=True)
        if linked.parent.name != "worktrees":
            return None
        common = linked.parent.parent
        backlink = linked / metadata_text(linked / "gitdir")
        linked_common = linked / metadata_text(linked / "commondir")
        if (backlink.name != ".git" or str(backlink.parent.resolve(strict=True)) != str(checkout)
                or str(linked_common.resolve(strict=True)) != str(common)):
            return None
        primary = common.parent
        primary_git = primary / ".git"
        actual = primary_git.resolve(strict=True) if primary_git.is_dir() else gitdir(primary_git).resolve(strict=True)
        return primary if str(actual) == str(common) else None
    except (OSError, UnicodeError, ValueError):
        return None


def is_workspace_trusted(workspace_root: Path | None) -> bool:
    """Chosen roots and validated linked checkouts share the current trust ledger."""

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
    trusted_roots = []
    for trusted_root in _resolved_trusted_roots(values):
        trusted_roots.append(trusted_root)
        try:
            resolved.relative_to(trusted_root)
            return True
        except ValueError:
            continue
    primary = _worktree_trust_root(resolved)
    return primary is not None and any(primary.is_relative_to(root) for root in trusted_roots)
