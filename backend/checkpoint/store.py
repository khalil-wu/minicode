from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from backend.config import DATA_ROOT
from backend.atomic_io import atomic_write_bytes, atomic_write_text
from filelock import FileLock

logger = logging.getLogger(__name__)

CHECKPOINT_DATA_DIR = DATA_ROOT / "checkpoints"


class CheckpointCorruptError(RuntimeError):
    """The checkpoint file exists but cannot be read or parsed.

    Distinct from "not found": callers and users must be able to tell a
    missing checkpoint from one that was created and later became unreadable.
    """


@dataclass(frozen=True)
class CheckpointFileSnapshot:
    path: str
    existed: bool
    content: str | None = None
    encoding: str = "utf-8"
    # Sidecar blob file name (under the store's blobs/ directory) holding the
    # raw byte copy. ``content`` remains populated only on legacy records that
    # inlined base64 payloads before the sidecar migration.
    blob: str | None = None


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
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["files"] = [asdict(item) for item in self.files]
        return payload

    def to_public_dict(self) -> dict[str, Any]:
        """Return checkpoint metadata without exposing rewind file contents."""

        return {
            "id": self.id,
            "conversation_id": self.conversation_id,
            "session_id": self.session_id,
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "workspace_root": self.workspace_root,
            "paths": list(self.paths),
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "CheckpointRecord":
        files = [
            CheckpointFileSnapshot(
                path=str(item.get("path", "")),
                existed=bool(item.get("existed", False)),
                content=item.get("content") if item.get("content") is not None else None,
                encoding=str(item.get("encoding") or "utf-8"),
                blob=str(item.get("blob")) if item.get("blob") else None,
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
            created_at=str(payload.get("created_at", "")),
            metadata=dict(payload.get("metadata") or {}),
        )


class CheckpointStore:
    def __init__(self, root: Path | None = None) -> None:
        self._root = root or CHECKPOINT_DATA_DIR
        self._root.mkdir(parents=True, exist_ok=True)
        # cc's fileHistory keeps snapshot payloads as byte-exact file copies in
        # a per-session directory next to its metadata state; blobs/ is the
        # MiniCode equivalent, shared by every session under this data root.
        self._blobs = self._root / "blobs"
        self._blobs.mkdir(parents=True, exist_ok=True)
        self._thread_lock = threading.RLock()
        self._process_lock = FileLock(str(self._root / ".checkpoints.lock"), timeout=60)

    def _locked(self) -> "_CheckpointLock":
        return _CheckpointLock(self._thread_lock, self._process_lock)

    def save(self, record: CheckpointRecord) -> CheckpointRecord:
        with self._locked():
            path = self._path_for(record.id)
            atomic_write_text(
                path,
                json.dumps(record.to_dict(), ensure_ascii=False, indent=2) + "\n",
            )
        return record

    def get(self, checkpoint_id: str) -> CheckpointRecord | None:
        checkpoint_id = checkpoint_id.strip()
        if not checkpoint_id:
            return None
        with self._locked():
            path = self._path_for(checkpoint_id)
            if not path.exists():
                return None
            try:
                return CheckpointRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError) as exc:
                # Swallowing this as None made a corrupt checkpoint report
                # "not found", hiding real data loss from the user.
                logger.error(
                    "Checkpoint file %s is corrupt: %s", path, exc
                )
                raise CheckpointCorruptError(
                    f"Checkpoint '{checkpoint_id}' exists but is corrupt and cannot be loaded: {exc}"
                ) from exc

    def list_for_conversation(
        self,
        conversation_id: str,
        *,
        limit: int | None = 50,
    ) -> list[CheckpointRecord]:
        if limit is not None and int(limit) <= 0:
            return []
        records: list[CheckpointRecord] = []
        with self._locked():
            paths: list[tuple[int, Path]] = []
            for path in self._root.glob("*.json"):
                try:
                    paths.append((path.stat().st_mtime_ns, path))
                except OSError:
                    continue
            for _stamp, path in sorted(paths, reverse=True):
                try:
                    record = CheckpointRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))
                except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
                    raise CheckpointCorruptError(
                        f"Checkpoint file {path.name} is corrupt and cannot be listed: {exc}"
                    ) from exc
                if record.conversation_id == conversation_id:
                    records.append(record)
                    if limit is not None and len(records) >= int(limit):
                        break
        return records

    def delete_for_conversation(self, conversation_id: str) -> int:
        """Remove every file-rewind checkpoint owned by one conversation."""
        owner = str(conversation_id or "").strip()
        if not owner:
            return 0
        removed = 0
        with self._locked():
            for path in self._root.glob("*.json"):
                try:
                    record = CheckpointRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))
                except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
                    raise CheckpointCorruptError(
                        f"Checkpoint file {path.name} is corrupt and cannot be deleted safely"
                    ) from exc
                if record.conversation_id != owner:
                    continue
                try:
                    path.unlink()
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    raise CheckpointCorruptError(
                        f"Checkpoint file {path.name} could not be deleted"
                    ) from exc
                for snapshot in record.files:
                    self._unlink_blob(snapshot.blob)
                removed += 1
        return removed

    def write_blob(self, name: str, data: bytes) -> None:
        """Store a byte-exact snapshot payload (cc createBackup copyFile)."""
        atomic_write_bytes(self._blob_path(name), data)

    def read_blob(self, name: str) -> bytes:
        try:
            return self._blob_path(name).read_bytes()
        except OSError as exc:
            raise CheckpointCorruptError(
                f"Checkpoint blob '{name}' exists in metadata but cannot be read: {exc}"
            ) from exc

    def _unlink_blob(self, name: str | None) -> None:
        if not name:
            return
        try:
            self._blob_path(name).unlink()
        except FileNotFoundError:
            pass
        except OSError:
            # Orphaned blobs are garbage, never a reason to fail a delete.
            logger.debug("Failed to remove checkpoint blob %s", name, exc_info=True)

    def _blob_path(self, name: str) -> Path:
        clean = str(name or "").strip()
        if not clean or any(ch not in "0123456789abcdef" for ch in clean):
            raise ValueError("Invalid checkpoint blob name")
        return self._blobs / clean

    def _path_for(self, checkpoint_id: str) -> Path:
        clean = str(checkpoint_id or "").strip()
        if not clean or clean in {".", ".."}:
            raise ValueError("checkpoint_id is required")
        if any(
            ch not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
            for ch in clean
        ):
            raise ValueError("Invalid checkpoint_id")
        return self._root / f"{clean}.json"


class _CheckpointLock:
    def __init__(self, thread_lock: Any, process_lock: FileLock) -> None:
        self._thread_lock = thread_lock
        self._process_lock = process_lock

    def __enter__(self) -> "_CheckpointLock":
        self._thread_lock.acquire()
        try:
            self._process_lock.acquire()
        except Exception:
            self._thread_lock.release()
            raise
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        try:
            self._process_lock.release()
        finally:
            self._thread_lock.release()
