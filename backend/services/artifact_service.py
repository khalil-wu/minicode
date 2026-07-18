from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from backend.agent.message import AgentEvent


@dataclass(frozen=True)
class ArtifactContentResult:
    artifact_id: str
    content: Any
    preview: str
    media_type: str
    purpose: str

    def to_event(self) -> AgentEvent:
        data: dict[str, Any] = {
            "artifact_id": self.artifact_id,
            "content": self.content,
            "preview": self.preview,
        }
        if self.purpose:
            data["purpose"] = self.purpose
        if self.media_type:
            data["media_type"] = self.media_type
            data["url"] = f"data:{self.media_type};base64,{self.content}"
        return AgentEvent(type="artifact_content", data=data)


def read_artifact_content(
    artifact_store: Any,
    attachment_store: Any,
    artifact_id: str,
    *,
    purpose: str = "",
) -> ArtifactContentResult:
    clean_artifact_id = str(artifact_id or "")
    content = artifact_store.get(clean_artifact_id)
    meta = artifact_store.get_meta(clean_artifact_id)
    preview = artifact_store.get_preview(clean_artifact_id) or ""
    media_type = "image/png" if getattr(meta, "type", "") == "image" else ""

    attachment_metadata = attachment_store.get_metadata(clean_artifact_id)
    attachment = attachment_metadata.get("attachment") if isinstance(attachment_metadata, dict) else None
    if isinstance(attachment, dict):
        attachment_media_type = str(attachment.get("media_type") or "").strip()
        attachment_kind = str(attachment.get("kind") or "").strip()
        native_data = str(attachment.get("data") or "").strip()
        if attachment_kind == "image" or attachment_media_type.startswith("image/"):
            media_type = attachment_media_type or media_type or "image/png"
            if native_data:
                content = native_data
        if not preview:
            preview = attachment_store.get_preview(clean_artifact_id) or ""

    if content is None:
        content = attachment_store.get(clean_artifact_id)
        if not preview:
            preview = attachment_store.get_preview(clean_artifact_id) or ""
    if content is None:
        raise ValueError(f"Artifact '{clean_artifact_id}' does not exist or has been cleared")

    return ArtifactContentResult(
        artifact_id=clean_artifact_id,
        content=content,
        preview=preview,
        media_type=media_type,
        purpose=str(purpose or "").strip(),
    )
