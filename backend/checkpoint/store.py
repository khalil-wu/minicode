from __future__ import annotations

import json
import hashlib
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
MAX_RETAINED_FILE_CHECKPOINTS = 100


def checkpoint_owner_key(conversation_id: str) -> str:
    return hashlib.sha256(str(conversation_id or "").encode("utf-8")).hexdigest()[:16]


class CheckpointCorruptError(RuntimeError):
    """The checkpoint file exists but cannot be read or parsed.

    Distinct from "not found": callers and users must be able to tell a
    missing checkpoint from one that was created and later became unreadable.
    """


@dataclass(frozen=True)
class CheckpointScanStatus:
    corrupt_files: tuple[str, ...] = ()

    @property
    def degraded(self) -> bool:
        return bool(self.corrupt_files)


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
        self._records = self._root / "records"
        self._records.mkdir(parents=True, exist_ok=True)
        self._thread_lock = threading.RLock()
        self._process_lock = FileLock(str(self._root / ".checkpoints.lock"), timeout=60)
        self.scan_status = CheckpointScanStatus()

    def _locked(self) -> "_CheckpointLock":
        return _CheckpointLock(self._thread_lock, self._process_lock)

    def save(self, record: CheckpointRecord) -> CheckpointRecord:
        with self._locked():
            embedded_owner = self._owner_key_from_checkpoint_id(record.id)
            expected_owner = checkpoint_owner_key(record.conversation_id)
            if embedded_owner and embedded_owner != expected_owner:
                raise ValueError("Checkpoint id does not match its conversation owner")
            path = self._path_for(record.id)
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(
                path,
                json.dumps(record.to_dict(), ensure_ascii=False, indent=2) + "\n",
            )
            if embedded_owner:
                self._prune_owner_unlocked(embedded_owner)
        return record

    def get(self, checkpoint_id: str) -> CheckpointRecord | None:
        checkpoint_id = checkpoint_id.strip()
        if not checkpoint_id:
            return None
        with self._locked():
            for path in self._candidate_paths_for(checkpoint_id):
                if not path.exists():
                    continue
                try:
                    return self._read_record(path)
                except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
                    # A direct lookup names one checkpoint, so corruption must
                    # remain a user-visible error rather than "not found".
                    logger.error("Checkpoint file %s is corrupt: %s", path, exc)
                    raise CheckpointCorruptError(
                        f"Checkpoint '{checkpoint_id}' exists but is corrupt and cannot be loaded: {exc}"
                    ) from exc
            return None

    def list_for_conversation(
        self,
        conversation_id: str,
        *,
        limit: int | None = 50,
    ) -> list[CheckpointRecord]:
        if limit is not None and int(limit) <= 0:
            return []
        owner = str(conversation_id or "").strip()
        records: list[CheckpointRecord] = []
        with self._locked():
            corrupt_files: list[str] = []
            paths: list[tuple[int, Path]] = []
            for path in self._conversation_paths(owner):
                try:
                    paths.append((path.stat().st_mtime_ns, path))
                except OSError as exc:
                    corrupt_files.append(self._path_label(path))
                    logger.warning("Cannot inspect checkpoint file %s: %s", path, exc)
                    continue
            for _stamp, path in sorted(paths, reverse=True):
                try:
                    record = self._read_record(path)
                except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
                    corrupt_files.append(self._path_label(path))
                    logger.warning("Skipping corrupt checkpoint file %s: %s", path, exc)
                    continue
                if record.conversation_id == owner:
                    records.append(record)
                    if limit is not None and len(records) >= int(limit):
                        break
            self.scan_status = CheckpointScanStatus(tuple(corrupt_files))
        return records

    def delete_for_conversation(self, conversation_id: str) -> int:
        """Remove every file-rewind checkpoint owned by one conversation."""
        owner = str(conversation_id or "").strip()
        if not owner:
            return 0
        removed = 0
        with self._locked():
            corrupt_files: list[str] = []
            owner_dir = self._owner_dir(checkpoint_owner_key(owner))
            owner_paths = list(owner_dir.glob("*.json")) if owner_dir.exists() else []
            legacy_paths = list(self._root.glob("*.json"))
            for path in [*owner_paths, *legacy_paths]:
                try:
                    record = self._read_record(path)
                except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
                    corrupt_files.append(self._path_label(path))
                    if path.parent == owner_dir:
                        logger.warning(
                            "Deleting corrupt checkpoint file from its isolated owner directory %s: %s",
                            path,
                            exc,
                        )
                        path.unlink(missing_ok=True)
                        removed += 1
                    else:
                        logger.warning(
                            "Skipping corrupt legacy checkpoint with unknown owner %s: %s",
                            path,
                            exc,
                        )
                    continue
                if record.conversation_id != owner:
                    continue
                self._delete_record_unlocked(path, record)
                removed += 1
            if owner_dir.exists() and not any(owner_dir.iterdir()):
                owner_dir.rmdir()
            self.scan_status = CheckpointScanStatus(tuple(corrupt_files))
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
        clean = self._validate_checkpoint_id(checkpoint_id)
        owner_key = self._owner_key_from_checkpoint_id(clean)
        if owner_key:
            return self._owner_dir(owner_key) / f"{clean}.json"
        return self._root / f"{clean}.json"

    def _candidate_paths_for(self, checkpoint_id: str) -> list[Path]:
        clean = self._validate_checkpoint_id(checkpoint_id)
        primary = self._path_for(clean)
        legacy = self._root / f"{clean}.json"
        return [primary] if primary == legacy else [primary, legacy]

    def _conversation_paths(self, conversation_id: str) -> list[Path]:
        owner_dir = self._owner_dir(checkpoint_owner_key(conversation_id))
        isolated = list(owner_dir.glob("*.json")) if owner_dir.exists() else []
        return [*isolated, *self._root.glob("*.json")]

    def _owner_dir(self, owner_key: str) -> Path:
        return self._records / owner_key

    @staticmethod
    def _owner_key_from_checkpoint_id(checkpoint_id: str) -> str:
        parts = str(checkpoint_id or "").split("_")
        if (
            len(parts) == 3
            and parts[0] == "chk"
            and len(parts[1]) == 16
            and len(parts[2]) == 12
            and all(ch in "0123456789abcdef" for ch in parts[1])
            and all(ch in "0123456789abcdef" for ch in parts[2])
        ):
            return parts[1]
        return ""

    @staticmethod
    def _validate_checkpoint_id(checkpoint_id: str) -> str:
        clean = str(checkpoint_id or "").strip()
        if not clean or clean in {".", ".."}:
            raise ValueError("checkpoint_id is required")
        if any(
            ch not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
            for ch in clean
        ):
            raise ValueError("Invalid checkpoint_id")
        return clean

    @staticmethod
    def _read_record(path: Path) -> CheckpointRecord:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("checkpoint payload must be an object")
        return CheckpointRecord.from_dict(payload)

    def _delete_record_unlocked(self, path: Path, record: CheckpointRecord) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise CheckpointCorruptError(
                f"Checkpoint file {path.name} could not be deleted"
            ) from exc
        for snapshot in record.files:
            self._unlink_blob(snapshot.blob)

    def _prune_owner_unlocked(
        self,
        owner_key: str,
        *,
        keep: int = MAX_RETAINED_FILE_CHECKPOINTS,
    ) -> None:
        owner_dir = self._owner_dir(owner_key)
        paths = sorted(
            owner_dir.glob("*.json"),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )
        for path in paths[keep:]:
            try:
                record = self._read_record(path)
            except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
                logger.warning("Deleting corrupt expired checkpoint file %s: %s", path, exc)
                path.unlink(missing_ok=True)
                continue
            self._delete_record_unlocked(path, record)

    def _path_label(self, path: Path) -> str:
        try:
            return path.relative_to(self._root).as_posix()
        except ValueError:
            return path.name


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
