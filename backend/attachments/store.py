from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any

from backend.config import DATA_ROOT
from backend.atomic_io import atomic_write_text, file_mutation_locks
from backend.owner_scope import (
    OwnerScope,
    grant_owner_scope,
    normalize_owner_scopes,
    owner_scope_matches,
    remove_conversation_scopes,
)


ATTACHMENT_DATA_DIR = DATA_ROOT / "attachments"
ATTACHMENT_INDEX_FILE = "index.json"
ARTIFACT_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
# Upload size limits are enforced in bytes on the wire (api/routes_chat.py)
# and in characters once content has been decoded into text (below). Keeping
# the two units distinct prevents a UTF-8 multi-byte file from being measured
# against the wrong yardstick.
MAX_ATTACHMENT_CONTENT_BYTES = 50 * 1024 * 1024
MAX_ATTACHMENT_CONTENT_CHARS = 50 * 1024 * 1024
MAX_ATTACHMENT_METADATA_CHARS = 256 * 1024
MAX_ATTACHMENT_NATIVE_DATA_CHARS = ((MAX_ATTACHMENT_CONTENT_CHARS + 2) // 3) * 4
OWNER_SCOPE_VERSION = 1


def _metadata_owner_scopes(metadata: dict[str, Any]) -> tuple[OwnerScope, ...]:
    raw_conversation_ids = metadata.get("conversation_ids")
    conversation_ids = (
        [str(value) for value in raw_conversation_ids]
        if isinstance(raw_conversation_ids, list)
        else []
    )
    strict = (
        "owner_scopes" in metadata
        or metadata.get("owner_scope_version") is not None
    )
    return normalize_owner_scopes(
        metadata.get("owner_scopes"),
        conversation_id=str(metadata.get("conversation_id") or ""),
        conversation_ids=conversation_ids,
        workspace_root=metadata.get("workspace_root"),
        strict=strict,
    )


def _attachment_owner_matches(
    scopes: tuple[OwnerScope, ...],
    conversation_id: str,
    workspace_root: str,
) -> bool:
    """Require one exact conversation/workspace grant for attachment bytes.

    Uploaded native data is private input, not an ambient conversation cache.
    Matching only a conversation id would let a conversation rebound to a
    different workspace read the old workspace's attachment body.  Align with
    ArtifactStore and the MiniCode owner boundary: a fork or intentional
    workspace migration must add an explicit composite grant first.
    """

    return owner_scope_matches(scopes, conversation_id, workspace_root)


def _write_owner_scopes(
    metadata: dict[str, Any],
    scopes: tuple[OwnerScope, ...],
) -> None:
    """Persist composite grants and keep legacy projections readable."""

    owners = list(
        dict.fromkeys(scope.conversation_id for scope in scopes if scope.conversation_id)
    )
    metadata["owner_scope_version"] = OWNER_SCOPE_VERSION
    metadata["owner_scopes"] = [scope.to_json() for scope in scopes]
    metadata["conversation_id"] = owners[0] if owners else ""
    metadata["conversation_ids"] = owners
    metadata["workspace_root"] = scopes[0].workspace_root if scopes else ""


class AttachmentStore:
    """Durable attachment content store for uploaded files."""

    def __init__(self, base_dir: Path | None = None) -> None:
        self._base_dir = Path(base_dir or ATTACHMENT_DATA_DIR)
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._index_path = self._base_dir / ATTACHMENT_INDEX_FILE
        self._lock = threading.RLock()
        self._index = self._load_index()
        self._index_ready = self._index_path.exists()

    def save(
        self,
        *,
        artifact_id: str,
        content: str,
        metadata: dict[str, Any] | None = None,
        native_data: str = "",
    ) -> None:
        safe_id = self._safe_artifact_id(artifact_id)
        if len(content) > MAX_ATTACHMENT_CONTENT_CHARS:
            raise ValueError("Attachment content exceeds the 50 MB limit.")
        normalized_metadata = dict(metadata or {})
        owner_scopes = _metadata_owner_scopes(normalized_metadata)
        if owner_scopes:
            _write_owner_scopes(normalized_metadata, owner_scopes)
        metadata_payload = json.dumps(normalized_metadata, ensure_ascii=False)
        if len(metadata_payload) > MAX_ATTACHMENT_METADATA_CHARS:
            raise ValueError("Attachment metadata exceeds the 256 KB limit.")
        if len(native_data) > MAX_ATTACHMENT_NATIVE_DATA_CHARS:
            raise ValueError("Attachment native data exceeds the 50 MB limit.")
        payload = {
            "artifact_id": safe_id,
            "content": content,
            "metadata": json.loads(metadata_payload),
        }
        if native_data:
            payload["native_data"] = native_data
        path = self._path_for(safe_id)
        # Publish the attachment and its lookup aliases as one serialized
        # operation. The process-wide path lock is required because each
        # websocket/session owns its own AttachmentStore instance; an
        # instance-local lock would still let two sessions overwrite a stale
        # index.json snapshot.
        with self._lock, file_mutation_locks([self._index_path, path]):
            atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))
            # Rebuild from the newly published payload so aliases removed by
            # an overwrite do not remain as stale index entries.
            self._rebuild_index()

    def get(self, artifact_id: str, *, conversation_id: str = "", workspace_root: str = "") -> str | None:
        payload = self.get_payload(
            artifact_id,
            conversation_id=conversation_id,
            workspace_root=workspace_root,
        )
        if payload is None:
            return None
        return str(payload.get("content") or "")

    def get_payload(
        self,
        artifact_id: str,
        *,
        conversation_id: str = "",
        workspace_root: str = "",
    ) -> dict[str, Any] | None:
        try:
            path = self._path_for(artifact_id)
        except ValueError:
            return None
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        metadata = payload.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        try:
            scopes = _metadata_owner_scopes(metadata)
        except (TypeError, ValueError):
            return None
        if not _attachment_owner_matches(scopes, conversation_id, workspace_root):
            return None
        return payload

    def get_metadata(
        self,
        artifact_id: str,
        *,
        conversation_id: str = "",
        workspace_root: str = "",
    ) -> dict[str, Any]:
        payload = self.get_payload(
            artifact_id,
            conversation_id=conversation_id,
            workspace_root=workspace_root,
        )
        metadata = payload.get("metadata") if payload else None
        return dict(metadata) if isinstance(metadata, dict) else {}

    def get_native_data(
        self,
        artifact_id: str,
        *,
        conversation_id: str = "",
        workspace_root: str = "",
    ) -> str | None:
        payload = self.get_payload(
            artifact_id,
            conversation_id=conversation_id,
            workspace_root=workspace_root,
        )
        if payload is None:
            return None
        native_data = payload.get("native_data")
        return str(native_data) if isinstance(native_data, str) and native_data else None

    def find_payload(
        self,
        ref: str,
        *,
        conversation_id: str = "",
        workspace_root: str = "",
    ) -> dict[str, Any] | None:
        """Find an uploaded attachment by artifact_id, doc_id, or file name."""
        needle = str(ref or "").strip()
        if not needle:
            return None

        payload = self.get_payload(needle, conversation_id=conversation_id, workspace_root=workspace_root)
        if payload is not None:
            return payload

        with self._lock, file_mutation_locks([self._index_path]):
            # Another session may have published an alias since this store was
            # constructed. Reload the durable index before resolving it.
            self._index = self._load_index()
            indexed_id = self._index.get(needle.casefold())
        if indexed_id:
            indexed_payload = self.get_payload(indexed_id, conversation_id=conversation_id, workspace_root=workspace_root)
            if indexed_payload is not None:
                return indexed_payload
            # The durable alias can outlive its payload when another session
            # deletes or replaces the attachment between the index read and
            # this lookup. Force a rebuild instead of treating the stale id as
            # a successful index hit.
            indexed_id = ""

        with self._lock, file_mutation_locks([self._index_path]):
            self._index = self._load_index()
            if not indexed_id or not self._index_ready:
                self._rebuild_index()
            indexed_id = self._index.get(needle.casefold())
        if indexed_id:
            return self.get_payload(indexed_id, conversation_id=conversation_id, workspace_root=workspace_root) if indexed_id else None
        return None

    def _load_index(self) -> dict[str, str]:
        try:
            payload = json.loads(self._index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, dict):
            return {}
        return {
            str(key): str(value)
            for key, value in payload.items()
            if isinstance(key, str) and isinstance(value, str)
        }

    def _index_payload(self, payload: dict[str, Any]) -> None:
        artifact_id = str(payload.get("artifact_id") or "").strip()
        if not artifact_id:
            return
        refs = {artifact_id}
        metadata = payload.get("metadata")
        attachment = metadata.get("attachment") if isinstance(metadata, dict) else None
        if isinstance(attachment, dict):
            refs.update(
                str(attachment.get(key) or "").strip()
                for key in ("doc_id", "artifact_id", "file_name")
            )
        for ref in refs:
            if ref:
                self._index[ref.casefold()] = artifact_id

    def _persist_index(self) -> None:
        atomic_write_text(
            self._index_path,
            json.dumps(self._index, ensure_ascii=False, separators=(",", ":")),
        )

    def _rebuild_index(self) -> None:
        self._index = {}
        for path in sorted(self._base_dir.glob("*.json"), reverse=True):
            if path == self._index_path:
                continue
            try:
                candidate = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(candidate, dict):
                continue
            self._index_payload(candidate)
        self._persist_index()
        self._index_ready = True

    def resolve_content(
        self,
        ref: str,
        *,
        conversation_id: str = "",
        workspace_root: str = "",
    ) -> tuple[str, str, dict[str, Any]] | None:
        """Resolve an attachment reference to artifact_id, content, and metadata."""
        payload = self.find_payload(ref, conversation_id=conversation_id, workspace_root=workspace_root)
        if payload is None:
            return None
        artifact_id = str(payload.get("artifact_id") or "").strip()
        content = str(payload.get("content") or "")
        metadata = payload.get("metadata")
        return artifact_id, content, dict(metadata) if isinstance(metadata, dict) else {}

    def get_preview(
        self,
        artifact_id: str,
        lines: int = 5,
        *,
        conversation_id: str = "",
        workspace_root: str = "",
    ) -> str | None:
        payload = self.get_payload(
            artifact_id,
            conversation_id=conversation_id,
            workspace_root=workspace_root,
        )
        if payload is None:
            return None
        # Native-only image/document attachments have no textual preview.
        # Do not turn their empty storage field into a misleading successful
        # preview; callers should use ``get_native_data``/the artifact URL.
        content = str(payload.get("content") or "")
        if not content and payload.get("native_data"):
            return None
        content_lines = content.split("\n")
        preview = "\n".join(content_lines[:lines])
        if len(content_lines) > lines:
            preview += f"\n... ({len(content_lines)} lines total)"
        return preview

    def delete_for_conversation(self, conversation_id: str) -> int:
        """Delete uploaded attachment payloads owned by one conversation."""
        owner = str(conversation_id or "").strip()
        if not owner:
            return 0
        removed = 0
        candidate_paths = [path for path in self._base_dir.glob("*.json") if path != self._index_path]
        with self._lock, file_mutation_locks([self._index_path, *candidate_paths]):
            self._rebuild_index()
            for path in self._base_dir.glob("*.json"):
                if path == self._index_path:
                    continue
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if not isinstance(payload, dict):
                    continue
                metadata = payload.get("metadata")
                if not isinstance(metadata, dict):
                    continue
                try:
                    scopes = _metadata_owner_scopes(metadata)
                except (TypeError, ValueError):
                    continue
                if not any(scope.conversation_id == owner for scope in scopes):
                    continue
                remaining_scopes = remove_conversation_scopes(scopes, owner)
                if remaining_scopes:
                    _write_owner_scopes(metadata, remaining_scopes)
                    payload["metadata"] = metadata
                    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))
                    continue
                try:
                    path.unlink()
                except FileNotFoundError:
                    continue
                removed += 1
            self._index_ready = False
            self._rebuild_index()
        return removed

    def share_for_conversation(
        self,
        source_conversation_id: str,
        target_conversation_id: str,
        workspace_root: str | Path | None = None,
    ) -> int:
        """Grant a cloned/forked transcript access to immutable source uploads."""

        source = str(source_conversation_id or "").strip()
        target = str(target_conversation_id or "").strip()
        if not source or not target or source == target:
            return 0
        shared = 0
        candidate_paths = [path for path in self._base_dir.glob("*.json") if path != self._index_path]
        with self._lock, file_mutation_locks([self._index_path, *candidate_paths]):
            self._rebuild_index()
            for path in self._base_dir.glob("*.json"):
                if path == self._index_path:
                    continue
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if not isinstance(payload, dict):
                    continue
                metadata = payload.get("metadata")
                if not isinstance(metadata, dict):
                    continue
                try:
                    scopes = _metadata_owner_scopes(metadata)
                except (TypeError, ValueError):
                    continue
                updated_scopes = grant_owner_scope(
                    scopes,
                    source_conversation_id=source,
                    target_conversation_id=target,
                    target_workspace_root=workspace_root,
                )
                if updated_scopes == scopes:
                    continue
                _write_owner_scopes(metadata, updated_scopes)
                payload["metadata"] = metadata
                atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))
                shared += 1
            self._index_ready = False
            self._rebuild_index()
        return shared

    def _path_for(self, artifact_id: str) -> Path:
        safe_id = self._safe_artifact_id(artifact_id)
        return self._base_dir / f"{safe_id}.json"

    @staticmethod
    def _safe_artifact_id(artifact_id: str) -> str:
        safe_id = str(artifact_id or "").strip()
        if not ARTIFACT_ID_RE.fullmatch(safe_id):
            raise ValueError("artifact_id may only contain letters, numbers, '_' and '-'")
        return safe_id

