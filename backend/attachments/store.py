from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from backend.config import DATA_ROOT
from backend.atomic_io import atomic_write_text


ATTACHMENT_DATA_DIR = DATA_ROOT / "attachments"
ATTACHMENT_INDEX_FILE = "index.json"
ARTIFACT_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
MAX_ATTACHMENT_CONTENT_CHARS = 50 * 1024 * 1024
MAX_ATTACHMENT_METADATA_CHARS = 256 * 1024
MAX_ATTACHMENT_NATIVE_DATA_CHARS = ((MAX_ATTACHMENT_CONTENT_CHARS + 2) // 3) * 4


class AttachmentStore:
    """Durable attachment content store for uploaded files."""

    def __init__(self, base_dir: Path | None = None) -> None:
        self._base_dir = Path(base_dir or ATTACHMENT_DATA_DIR)
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._index_path = self._base_dir / ATTACHMENT_INDEX_FILE
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
        metadata_payload = json.dumps(dict(metadata or {}), ensure_ascii=False)
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
        atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))
        self._index_payload(payload)
        self._persist_index()
        self._index_ready = True

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
        if conversation_id and str(metadata.get("conversation_id") or "") != conversation_id:
            return None
        stored_workspace = str(metadata.get("workspace_root") or "")
        if workspace_root:
            if not stored_workspace:
                return None
            try:
                if Path(stored_workspace).resolve() != Path(workspace_root).resolve():
                    return None
            except OSError:
                if stored_workspace != workspace_root:
                    return None
        return payload

    def get_metadata(self, artifact_id: str) -> dict[str, Any]:
        payload = self.get_payload(artifact_id)
        metadata = payload.get("metadata") if payload else None
        return dict(metadata) if isinstance(metadata, dict) else {}

    def get_native_data(self, artifact_id: str) -> str | None:
        payload = self.get_payload(artifact_id)
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

        indexed_id = self._index.get(needle.casefold())
        if indexed_id:
            indexed_payload = self.get_payload(indexed_id, conversation_id=conversation_id, workspace_root=workspace_root)
            if indexed_payload is not None:
                return indexed_payload

        if not self._index_ready:
            self._rebuild_index()
            indexed_id = self._index.get(needle.casefold())
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

    def get_preview(self, artifact_id: str, lines: int = 5) -> str | None:
        content = self.get(artifact_id)
        if content is None:
            return None
        content_lines = content.split("\n")
        preview = "\n".join(content_lines[:lines])
        if len(content_lines) > lines:
            preview += f"\n... ({len(content_lines)} lines total)"
        return preview

    def _path_for(self, artifact_id: str) -> Path:
        safe_id = self._safe_artifact_id(artifact_id)
        return self._base_dir / f"{safe_id}.json"

    @staticmethod
    def _safe_artifact_id(artifact_id: str) -> str:
        safe_id = str(artifact_id or "").strip()
        if not ARTIFACT_ID_RE.fullmatch(safe_id):
            raise ValueError("artifact_id may only contain letters, numbers, '_' and '-'")
        return safe_id
