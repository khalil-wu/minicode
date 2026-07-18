from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


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

    def load_ids(self, *, limit: int, max_age_seconds: float = 86_400.0) -> list[str]:
        if limit <= 0 or not self.path.exists():
            return []

        now = time.time()
        ids: list[str] = []
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    raw = line.strip()
                    if not raw:
                        continue
                    try:
                        payload = json.loads(raw)
                    except json.JSONDecodeError:
                        logger.debug("Skipping malformed client command log line in %s", self.path)
                        continue
                    command_id = _clean_command_id(payload.get("client_command_id") if isinstance(payload, dict) else "")
                    if not command_id:
                        continue
                    created_at = payload.get("created_at") if isinstance(payload, dict) else None
                    if isinstance(created_at, (int, float)) and max_age_seconds > 0 and now - float(created_at) > max_age_seconds:
                        continue
                    ids.append(command_id)
        except OSError as exc:
            logger.debug("Failed to load client command log for %s: %s", self.session_id, exc)
            return []

        return list(dict.fromkeys(ids[-limit:]))

    def append(self, client_command_id: str, *, command_type: str = "") -> None:
        command_id = _clean_command_id(client_command_id)
        if not command_id:
            return
        self.root_dir.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "client_command_id": command_id,
            "command_type": str(command_type or "")[:128],
            "created_at": time.time(),
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")

    def rewrite_ids(self, client_command_ids: list[str]) -> None:
        self.root_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        now = time.time()
        with tmp_path.open("w", encoding="utf-8") as handle:
            for command_id in client_command_ids:
                clean = _clean_command_id(command_id)
                if not clean:
                    continue
                payload = {"client_command_id": clean, "created_at": now}
                handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
                handle.write("\n")
        tmp_path.replace(self.path)


def _clean_command_id(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    command_id = value.strip()[:128]
    if not command_id:
        return ""
    if not all(char.isalnum() or char in {"_", "-", ":", "."} for char in command_id):
        return ""
    return command_id
