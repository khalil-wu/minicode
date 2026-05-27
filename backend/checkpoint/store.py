from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from backend.config import PROJECT_ROOT

CHECKPOINT_DATA_DIR = PROJECT_ROOT / "data" / "checkpoints"


@dataclass(frozen=True)
class CheckpointFileSnapshot:
    path: str
    existed: bool
    content: str | None = None
    encoding: str = "utf-8"


@dataclass(frozen=True)
class CheckpointRecord:
    id: str
    conversation_id: str
    session_id: str
    tool_call_id: str
    tool_name: str
    workspace_root: str
    paths: list[str]
    files: list[CheckpointFileSnapshot]
    git_head: str | None = None
    git_stash_ref: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["files"] = [asdict(item) for item in self.files]
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "CheckpointRecord":
        files = [
            CheckpointFileSnapshot(
                path=str(item.get("path", "")),
                existed=bool(item.get("existed", False)),
                content=item.get("content") if item.get("content") is not None else None,
                encoding=str(item.get("encoding") or "utf-8"),
            )
            for item in payload.get("files", [])
            if isinstance(item, dict)
        ]
        return cls(
            id=str(payload.get("id", "")),
            conversation_id=str(payload.get("conversation_id", "")),
            session_id=str(payload.get("session_id", "")),
            tool_call_id=str(payload.get("tool_call_id", "")),
            tool_name=str(payload.get("tool_name", "")),
            workspace_root=str(payload.get("workspace_root", "")),
            paths=[str(item) for item in payload.get("paths", [])],
            files=files,
            git_head=payload.get("git_head") or None,
            git_stash_ref=payload.get("git_stash_ref") or None,
            created_at=str(payload.get("created_at", "")),
            metadata=dict(payload.get("metadata") or {}),
        )


class CheckpointStore:
    def __init__(self, root: Path | None = None) -> None:
        self._root = root or CHECKPOINT_DATA_DIR
        self._root.mkdir(parents=True, exist_ok=True)

    def save(self, record: CheckpointRecord) -> CheckpointRecord:
        path = self._path_for(record.id)
        path.write_text(json.dumps(record.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return record

    def get(self, checkpoint_id: str) -> CheckpointRecord | None:
        checkpoint_id = checkpoint_id.strip()
        if not checkpoint_id:
            return None
        path = self._path_for(checkpoint_id)
        if not path.exists():
            return None
        try:
            return CheckpointRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            return None

    def list_for_conversation(self, conversation_id: str, *, limit: int = 50) -> list[CheckpointRecord]:
        records: list[CheckpointRecord] = []
        for path in sorted(self._root.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
            try:
                record = CheckpointRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
            if record.conversation_id == conversation_id:
                records.append(record)
                if len(records) >= limit:
                    break
        return records

    def _path_for(self, checkpoint_id: str) -> Path:
        safe = "".join(ch for ch in checkpoint_id if ch.isalnum() or ch in {"_", "-"}).strip()
        if not safe:
            safe = "checkpoint"
        return self._root / f"{safe}.json"
