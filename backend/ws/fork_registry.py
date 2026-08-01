"""Stable, replayable metadata for context branches.

The context object remains process-local, while branch identity and origin are
durable.  This prevents a fork from being identified by Python's ``id()`` and
gives a later session restore enough information to offer the branch again.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

logger = logging.getLogger(__name__)


def _safe(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", str(value or "")).strip("_") or "session"


@dataclass(frozen=True, slots=True)
class ForkRecord:
    fork_id: str
    parent_conversation_id: str
    message_index: int
    history_length: int
    estimated_tokens: int
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    status: str = "active"
    branch_conversation_id: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class ForkRegistry:
    def __init__(self, *, session_id: str, root_dir: Path) -> None:
        self.path = Path(root_dir) / f"{_safe(session_id)}.json"
        self._records: dict[str, ForkRecord] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("Unable to load fork registry %s: %s", self.path, exc)
            return
        for item in payload.get("forks", []) if isinstance(payload, dict) else []:
            if not isinstance(item, dict) or not str(item.get("fork_id") or "").strip():
                continue
            record = ForkRecord(
                fork_id=str(item["fork_id"]),
                parent_conversation_id=str(item.get("parent_conversation_id") or ""),
                message_index=int(item.get("message_index") or 0),
                history_length=int(item.get("history_length") or 0),
                estimated_tokens=int(item.get("estimated_tokens") or 0),
                created_at=str(item.get("created_at") or datetime.now(UTC).isoformat()),
                status=str(item.get("status") or "active"),
                branch_conversation_id=str(item.get("branch_conversation_id") or ""),
            )
            self._records[record.fork_id] = record

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        tmp_path.write_text(
            json.dumps(
                {"version": 1, "forks": [record.to_dict() for record in self._records.values()]},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        tmp_path.replace(self.path)

    def create(
        self,
        *,
        parent_conversation_id: str,
        message_index: int,
        history_length: int,
        estimated_tokens: int,
    ) -> ForkRecord:
        record = ForkRecord(
            fork_id=f"fork_{uuid4().hex[:16]}",
            parent_conversation_id=str(parent_conversation_id or ""),
            message_index=int(message_index),
            history_length=max(0, int(history_length)),
            estimated_tokens=max(0, int(estimated_tokens)),
        )
        self._records[record.fork_id] = record
        self._save()
        return record

    def get(self, fork_id: str) -> ForkRecord | None:
        return self._records.get(str(fork_id or "").strip())

    def bind_branch(self, fork_id: str, conversation_id: str) -> ForkRecord | None:
        key = str(fork_id or "").strip()
        current = self._records.get(key)
        if current is None:
            return None
        updated = ForkRecord(
            fork_id=current.fork_id,
            parent_conversation_id=current.parent_conversation_id,
            message_index=current.message_index,
            history_length=current.history_length,
            estimated_tokens=current.estimated_tokens,
            created_at=current.created_at,
            status=current.status,
            branch_conversation_id=str(conversation_id or "").strip(),
        )
        self._records[key] = updated
        self._save()
        return updated

    def list(self, *, parent_conversation_id: str = "") -> list[ForkRecord]:
        parent = str(parent_conversation_id or "").strip()
        records = [record for record in self._records.values() if not parent or record.parent_conversation_id == parent]
        return sorted(records, key=lambda record: record.created_at)
