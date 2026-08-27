from __future__ import annotations

import json
import threading
from pathlib import Path

from backend.atomic_io import atomic_write_text, file_mutation_locks
from backend.config import DATA_ROOT

WORKSPACE_STATE_FILE = DATA_ROOT / "active_workspace.json"
_active_workspace_root: Path | None = None
_WORKSPACE_STATE_LOCK = threading.RLock()


def _read_persisted_workspace_root() -> Path | None:
    with file_mutation_locks([WORKSPACE_STATE_FILE]):
        try:
            if not WORKSPACE_STATE_FILE.exists():
                return None
            raw = WORKSPACE_STATE_FILE.read_text(encoding="utf-8")
            payload = json.loads(raw)
            root = str(payload.get("root", "")).strip()
            if not root:
                return None
            return Path(root).resolve()
        except Exception:
            return None


def _write_persisted_workspace_root(root: Path | None) -> None:
    with file_mutation_locks([WORKSPACE_STATE_FILE]):
        try:
            WORKSPACE_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            if root is None:
                WORKSPACE_STATE_FILE.unlink(missing_ok=True)
                return
            atomic_write_text(
                WORKSPACE_STATE_FILE,
                json.dumps({"root": str(root.resolve())}, ensure_ascii=False, indent=2) + "\n",
            )
        except Exception:
            pass


def get_active_workspace_root(default_root: str | Path | None = None) -> Path:
    global _active_workspace_root
    with _WORKSPACE_STATE_LOCK:
        if _active_workspace_root is not None:
            if _active_workspace_root.exists() and _active_workspace_root.is_dir():
                return _active_workspace_root
            set_active_workspace_root(None)
            if default_root is not None:
                return Path(default_root).resolve()
            return Path.cwd().resolve()

        fallback = Path(default_root).resolve() if default_root is not None else Path.cwd().resolve()
        if not fallback.exists() or not fallback.is_dir():
            fallback = Path.cwd().resolve()

        persisted = _read_persisted_workspace_root()
        if persisted is not None:
            if persisted.exists() and persisted.is_dir():
                _active_workspace_root = persisted
                return persisted
            _write_persisted_workspace_root(None)

        return fallback


def get_explicit_active_workspace_root() -> Path | None:
    """Return an opened/persisted workspace, never an implicit process fallback."""
    global _active_workspace_root
    with _WORKSPACE_STATE_LOCK:
        if _active_workspace_root is not None:
            if _active_workspace_root.exists() and _active_workspace_root.is_dir():
                return _active_workspace_root
            set_active_workspace_root(None)
            return None

        persisted = _read_persisted_workspace_root()
        if persisted is not None and persisted.exists() and persisted.is_dir():
            _active_workspace_root = persisted
            return persisted
        if persisted is not None:
            _write_persisted_workspace_root(None)
        return None

def set_active_workspace_root(root: str | Path | None) -> Path | None:
    global _active_workspace_root
    with _WORKSPACE_STATE_LOCK:
        if root is None:
            _active_workspace_root = None
            _write_persisted_workspace_root(None)
            return None

        resolved = Path(root).resolve()
        _active_workspace_root = resolved
        _write_persisted_workspace_root(resolved)
        return resolved


def clear_active_workspace_root() -> None:
    set_active_workspace_root(None)
