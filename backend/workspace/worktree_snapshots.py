"""Durable snapshots of isolated worktrees, taken before destructive cleanup.

Mirrors the persistence shape of :mod:`backend.checkpoint.store`: one JSON file
per record. A snapshot captures the full working state (tracked + untracked) of a
worktree as a git commit object anchored by a ref under
``refs/minicode/wt-snapshots/<id>`` so it survives ``git worktree remove`` and is
safe from garbage collection. This store only persists the lightweight metadata
needed to find and restore that commit.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from backend.config import DATA_ROOT

WORKTREE_SNAPSHOT_DATA_DIR = DATA_ROOT / "worktree-snapshots"


@dataclass(frozen=True)
class WorktreeSnapshotRecord:
    """Metadata for one pre-deletion worktree snapshot."""

    id: str
    conversation_id: str = ""
    branch: str = ""
    original_path: str = ""
    main_repo_path: str = ""
    head: str | None = None
    snapshot_sha: str = ""
    snapshot_ref: str = ""
    label: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "WorktreeSnapshotRecord":
        return cls(
            id=str(payload.get("id", "")),
            conversation_id=str(payload.get("conversation_id", "")),
            branch=str(payload.get("branch", "")),
            original_path=str(payload.get("original_path", "")),
            main_repo_path=str(payload.get("main_repo_path", "")),
            head=payload.get("head") or None,
            snapshot_sha=str(payload.get("snapshot_sha", "")),
            snapshot_ref=str(payload.get("snapshot_ref", "")),
            label=str(payload.get("label", "")),
            created_at=str(payload.get("created_at", "")),
        )


class WorktreeSnapshotStore:
    """JSON-file-per-record store for worktree snapshots."""

    def __init__(self, root: Path | None = None) -> None:
        self._root = root or WORKTREE_SNAPSHOT_DATA_DIR
        self._root.mkdir(parents=True, exist_ok=True)

    def save(self, record: WorktreeSnapshotRecord) -> WorktreeSnapshotRecord:
        path = self._path_for(record.id)
        path.write_text(
            json.dumps(record.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return record

    def get(self, snapshot_id: str) -> WorktreeSnapshotRecord | None:
        snapshot_id = (snapshot_id or "").strip()
        if not snapshot_id:
            return None
        path = self._path_for(snapshot_id)
        if not path.exists():
            return None
        try:
            return WorktreeSnapshotRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            return None

    def list(self, conversation_id: str | None = None, *, limit: int = 100) -> list[WorktreeSnapshotRecord]:
        records: list[WorktreeSnapshotRecord] = []
        for path in sorted(self._root.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
            try:
                record = WorktreeSnapshotRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
            if conversation_id and record.conversation_id != conversation_id:
                continue
            records.append(record)
            if len(records) >= limit:
                break
        return records

    def _path_for(self, snapshot_id: str) -> Path:
        safe = "".join(ch for ch in snapshot_id if ch.isalnum() or ch in {"_", "-"}).strip()
        if not safe:
            safe = "snapshot"
        return self._root / f"{safe}.json"
