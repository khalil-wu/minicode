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

from backend.atomic_io import atomic_write_text, canonical_file_path_key, file_mutation_locks

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
        with file_mutation_locks([self.path]):
            self._load_unlocked()

    def _load_unlocked(self) -> None:
        self._records = {}
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("Unable to load fork registry %s: %s", self.path, exc)
            return
        raw_forks = payload.get("forks") if isinstance(payload, dict) else []
        if not isinstance(raw_forks, list):
            raw_forks = []
        for item in raw_forks:
            if not isinstance(item, dict) or not str(item.get("fork_id") or "").strip():
                continue
            try:
                record = ForkRecord(
                    fork_id=str(item["fork_id"]),
                    parent_conversation_id=str(item.get("parent_conversation_id") or ""),
                    message_index=max(0, int(item.get("message_index") or 0)),
                    history_length=max(0, int(item.get("history_length") or 0)),
                    estimated_tokens=max(0, int(item.get("estimated_tokens") or 0)),
                    created_at=str(item.get("created_at") or datetime.now(UTC).isoformat()),
                    status=str(item.get("status") or "active"),
                    branch_conversation_id=str(item.get("branch_conversation_id") or ""),
                )
            except (TypeError, ValueError, OverflowError):
                # A malformed record must not make the whole fixed session
                # unable to connect.  Keep the valid siblings and let the
                # next write compact the registry back to a valid shape.
                logger.warning("Skipping malformed fork record in %s", self.path)
                continue
            self._records[record.fork_id] = record

    def _save(self) -> None:
        atomic_write_text(
            self.path,
            json.dumps(
                {"version": 1, "forks": [record.to_dict() for record in self._records.values()]},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )

    def create(
        self,
        *,
        parent_conversation_id: str,
        message_index: int,
        history_length: int,
        estimated_tokens: int,
    ) -> ForkRecord:
        with file_mutation_locks([self.path]):
            self._load_unlocked()
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
        with file_mutation_locks([self.path]):
            self._load_unlocked()
            return self._records.get(str(fork_id or "").strip())

    def bind_branch(self, fork_id: str, conversation_id: str) -> ForkRecord | None:
        with file_mutation_locks([self.path]):
            self._load_unlocked()
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
        with file_mutation_locks([self.path]):
            self._load_unlocked()
            parent = str(parent_conversation_id or "").strip()
            records = [record for record in self._records.values() if not parent or record.parent_conversation_id == parent]
            return sorted(records, key=lambda record: record.created_at)

    def discard(self, fork_id: str) -> bool:
        with file_mutation_locks([self.path]):
            self._load_unlocked()
            key = str(fork_id or "").strip()
            if not key or self._records.pop(key, None) is None:
                return False
            self._save()
            return True

    def fork_ids_for_conversation(self, conversation_id: str) -> list[str]:
        with file_mutation_locks([self.path]):
            self._load_unlocked()
            return self._fork_ids_for_conversation_unlocked(conversation_id)

    def _fork_ids_for_conversation_unlocked(self, conversation_id: str) -> list[str]:
        owner = str(conversation_id or "").strip()
        if not owner:
            return []
        return [
            fork_id
            for fork_id, record in self._records.items()
            if record.parent_conversation_id == owner
            or record.branch_conversation_id == owner
        ]

    def delete_for_conversation(self, conversation_id: str) -> int:
        """Remove fork metadata that points from or to a deleted conversation."""
        owner = str(conversation_id or "").strip()
        if not owner:
            return 0
        with file_mutation_locks([self.path]):
            self._load_unlocked()
            fork_ids = self._fork_ids_for_conversation_unlocked(owner)
            for fork_id in fork_ids:
                self._records.pop(fork_id, None)
            if fork_ids:
                self._save()
            return len(fork_ids)

    def delete_for_conversation_across_sessions(self, conversation_id: str) -> int:
        """Remove durable fork metadata for a deleted chat from every session file."""

        owner = str(conversation_id or "").strip()
        if not owner:
            return 0
        removed = self.delete_for_conversation(owner)
        own_key = canonical_file_path_key(self.path)
        for registry_path in self.path.parent.glob("*.json"):
            if canonical_file_path_key(registry_path) == own_key:
                continue
            with file_mutation_locks([registry_path]):
                try:
                    payload = json.loads(registry_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if not isinstance(payload, dict) or not isinstance(payload.get("forks"), list):
                    continue
                retained: list[object] = []
                path_removed = 0
                for item in payload["forks"]:
                    if not isinstance(item, dict):
                        retained.append(item)
                        continue
                    if (
                        str(item.get("parent_conversation_id") or "") == owner
                        or str(item.get("branch_conversation_id") or "") == owner
                    ):
                        path_removed += 1
                    else:
                        retained.append(item)
                if not path_removed:
                    continue
                payload["forks"] = retained
                atomic_write_text(
                    registry_path,
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                )
                removed += path_removed
        return removed
