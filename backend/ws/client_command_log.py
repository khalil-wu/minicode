from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from backend.atomic_io import atomic_write_text, canonical_file_path_key
from filelock import FileLock

logger = logging.getLogger(__name__)

CLIENT_COMMAND_DEDUP_MAX_AGE_SECONDS = 86_400.0
_PATH_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[str, threading.RLock] = {}


def _path_lock(path: Path) -> threading.RLock:
    key = canonical_file_path_key(path)
    with _PATH_LOCKS_GUARD:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PATH_LOCKS[key] = lock
        return lock


def _process_lock(path: Path) -> FileLock:
    return FileLock(str(path.with_name(f".{path.name}.mutation.lock")), timeout=60)


class ClientCommandDedupStore:
    """Small JSONL-backed recent client command id store.

    This is not a durable command queue. It only preserves the idempotency
    window across WebSocketSession object recreation so resent commands do not
    re-run obvious side effects after a reconnect/backend refresh.
    """

    def __init__(self, *, session_id: str, root_dir: Path) -> None:
        self.session_id = session_id
        self.root_dir = Path(root_dir)
        safe_session_id = re.sub(r"[^A-Za-z0-9_-]+", "_", session_id).strip("_") or "session"
        self.path = self.root_dir / f"{safe_session_id}.jsonl"
        self._lock = _path_lock(self.path)
        self._file_lock = _process_lock(self.path)

    @contextmanager
    def _locked(self):
        with self._lock:
            with self._file_lock:
                yield

    def load_ids(
        self,
        *,
        limit: int,
        max_age_seconds: float = CLIENT_COMMAND_DEDUP_MAX_AGE_SECONDS,
    ) -> list[str]:
        if limit <= 0:
            return []

        with self._locked():
            if not self.path.exists():
                return []
            now = time.time()
            ids: list[str] = []
            should_rewrite = False
            try:
                with self.path.open("r", encoding="utf-8") as handle:
                    for line in handle:
                        raw = line.strip()
                        if not raw:
                            should_rewrite = True
                            continue
                        try:
                            payload = json.loads(raw)
                        except json.JSONDecodeError:
                            logger.debug("Skipping malformed client command log line in %s", self.path)
                            should_rewrite = True
                            continue
                        command_id = _clean_command_id(payload.get("client_command_id") if isinstance(payload, dict) else "")
                        if not command_id:
                            should_rewrite = True
                            continue
                        created_at = payload.get("created_at") if isinstance(payload, dict) else None
                        if isinstance(created_at, (int, float)) and max_age_seconds > 0 and now - float(created_at) > max_age_seconds:
                            should_rewrite = True
                            continue
                        ids.append(command_id)
            except OSError as exc:
                logger.debug("Failed to load client command log for %s: %s", self.session_id, exc)
                return []

            retained = list(dict.fromkeys(ids[-limit:]))
            if len(ids) != len(retained) or len(ids) > limit:
                should_rewrite = True
            if should_rewrite:
                try:
                    self._rewrite_ids_unlocked(retained)
                except OSError as exc:
                    logger.debug("Failed to compact client command log for %s: %s", self.session_id, exc)
            return retained

    def append(self, client_command_id: str, *, command_type: str = "") -> None:
        command_id = _clean_command_id(client_command_id)
        if not command_id:
            return
        with self._locked():
            self.root_dir.mkdir(parents=True, exist_ok=True)
            payload: dict[str, Any] = {
                "client_command_id": command_id,
                "command_type": str(command_type or "")[:128],
                "created_at": time.time(),
            }
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())

    def rewrite_ids(self, client_command_ids: list[str]) -> None:
        with self._locked():
            self._rewrite_ids_unlocked(client_command_ids)

    def _rewrite_ids_unlocked(self, client_command_ids: list[str]) -> None:
        now = time.time()
        lines: list[str] = []
        for command_id in client_command_ids:
            clean = _clean_command_id(command_id)
            if not clean:
                continue
            payload = {"client_command_id": clean, "created_at": now}
            lines.append(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        atomic_write_text(self.path, "".join(f"{line}\n" for line in lines))


def _clean_command_id(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    command_id = value.strip()[:128]
    if not command_id:
        return ""
    if not all(char.isalnum() or char in {"_", "-", ":", "."} for char in command_id):
        return ""
    return command_id


def cleanup_stale_client_command_logs(
    root_dir: Path,
    *,
    now: float | None = None,
    max_age_seconds: float = CLIENT_COMMAND_DEDUP_MAX_AGE_SECONDS,
) -> int:
    """Delete session logs whose entire idempotency window has expired."""

    root = Path(root_dir)
    if not root.exists() or max_age_seconds <= 0:
        return 0
    cutoff = float(time.time() if now is None else now) - float(max_age_seconds)
    removed = 0
    for path in root.glob("*.jsonl"):
        with _path_lock(path), _process_lock(path):
            try:
                if path.stat().st_mtime >= cutoff:
                    continue
                path.unlink(missing_ok=True)
                removed += 1
            except OSError as exc:
                logger.debug("Failed to clean stale client command log %s: %s", path, exc)
    return removed
