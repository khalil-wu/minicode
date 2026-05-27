from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from backend.config import PROJECT_ROOT


ATTACHMENT_DATA_DIR = PROJECT_ROOT / "data" / "attachments"
ARTIFACT_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class AttachmentStore:
    """Durable attachment content store for uploaded files."""

    def __init__(self, base_dir: Path | None = None) -> None:
        self._base_dir = Path(base_dir or ATTACHMENT_DATA_DIR)
        self._base_dir.mkdir(parents=True, exist_ok=True)

    def save(
        self,
        *,
        artifact_id: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        safe_id = self._safe_artifact_id(artifact_id)
        payload = {
            "artifact_id": safe_id,
            "content": content,
            "metadata": dict(metadata or {}),
        }
        self._path_for(safe_id).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def get(self, artifact_id: str) -> str | None:
        payload = self.get_payload(artifact_id)
        if payload is None:
            return None
        return str(payload.get("content") or "")

    def get_payload(self, artifact_id: str) -> dict[str, Any] | None:
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
        return payload if isinstance(payload, dict) else None

    def get_metadata(self, artifact_id: str) -> dict[str, Any]:
        payload = self.get_payload(artifact_id)
        metadata = payload.get("metadata") if payload else None
        return dict(metadata) if isinstance(metadata, dict) else {}

    def find_payload(self, ref: str) -> dict[str, Any] | None:
        """Find an uploaded attachment by artifact_id, doc_id, or file name."""
        needle = str(ref or "").strip()
        if not needle:
            return None

        payload = self.get_payload(needle)
        if payload is not None:
            return payload

        for path in sorted(self._base_dir.glob("*.json"), reverse=True):
            try:
                candidate = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(candidate, dict):
                continue
            if needle == str(candidate.get("artifact_id") or ""):
                return candidate
            metadata = candidate.get("metadata")
            attachment = metadata.get("attachment") if isinstance(metadata, dict) else None
            if not isinstance(attachment, dict):
                continue
            if needle in {
                str(attachment.get("doc_id") or ""),
                str(attachment.get("artifact_id") or ""),
                str(attachment.get("file_name") or ""),
            }:
                return candidate
        return None

    def resolve_content(self, ref: str) -> tuple[str, str, dict[str, Any]] | None:
        """Resolve an attachment reference to artifact_id, content, and metadata."""
        payload = self.find_payload(ref)
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
