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
    name: str = ""
    url: str = ""
    is_attachment: bool = False
    # Artifact reads are delivered asynchronously over the same socket as
    # agent events.  Carry the server-verified conversation owner so the
    # renderer can route the payload to the right workbench instead of
    # projecting a background conversation into the active Preview panel.
    conversation_id: str = ""
    workspace_root: str = ""
    request_id: str = ""

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
        if self.name:
            data["name"] = self.name
        if self.url:
            data["url"] = self.url
        elif self.media_type and not self.is_attachment:
            data["url"] = f"data:{self.media_type};base64,{self.content}"
        if self.is_attachment:
            data["is_attachment"] = True
        if self.conversation_id:
            data["conversation_id"] = self.conversation_id
        data["workspace_root"] = self.workspace_root
        if self.request_id:
            data["request_id"] = self.request_id
        return AgentEvent(type="artifact_content", data=data)


def read_artifact_content(
    artifact_store: Any,
    attachment_store: Any,
    artifact_id: str,
    *,
    purpose: str = "",
    conversation_id: str = "",
    workspace_root: str = "",
    request_id: str = "",
) -> ArtifactContentResult:
    clean_artifact_id = str(artifact_id or "")
    content = artifact_store.get(
        clean_artifact_id,
        conversation_id=conversation_id,
        workspace_root=workspace_root,
    )
    meta = artifact_store.get_meta(
        clean_artifact_id,
        conversation_id=conversation_id,
        workspace_root=workspace_root,
    )
    if content is None:
        meta = None
    preview = (
        artifact_store.get_preview(
            clean_artifact_id,
            conversation_id=conversation_id,
            workspace_root=workspace_root,
        )
        or ""
    )
    media_type = (
        str(getattr(meta, "media_type", "") or "").strip()
        or ("image/png" if getattr(meta, "type", "") == "image" else "")
    )
    name = ""
    is_attachment = False

    attachment_payload = attachment_store.get_payload(
        clean_artifact_id,
        conversation_id=conversation_id,
        workspace_root=workspace_root,
    )
    attachment_metadata = attachment_payload.get("metadata") if attachment_payload else {}
    attachment = attachment_metadata.get("attachment") if isinstance(attachment_metadata, dict) else None
    if isinstance(attachment, dict):
        is_attachment = True
        attachment_media_type = str(attachment.get("media_type") or "").strip()
        media_type = attachment_media_type or media_type
        name = str(attachment.get("file_name") or attachment.get("title") or "").strip()
        # Uploaded binary bodies are served by the session-owned HTTP preview
        # endpoint. Keep artifact_content reference/text-only so opening a large
        # image or PDF cannot stall the WebSocket event stream.
        content = str(attachment_payload.get("content") or "")
        if not preview:
            preview = attachment_store.get_preview(
                clean_artifact_id,
                conversation_id=conversation_id,
                workspace_root=workspace_root,
            ) or ""

    if content is None:
        content = str(attachment_payload.get("content") or "") if attachment_payload else None
        if not preview:
            preview = attachment_store.get_preview(
                clean_artifact_id,
                conversation_id=conversation_id,
                workspace_root=workspace_root,
            ) or ""
    if content is None:
        raise ValueError(f"Artifact '{clean_artifact_id}' does not exist or has been cleared")

    return ArtifactContentResult(
        artifact_id=clean_artifact_id,
        content=content,
        preview=preview,
        media_type=media_type,
        purpose=str(purpose or "").strip(),
        name=name,
        is_attachment=is_attachment,
        conversation_id=str(conversation_id or "").strip(),
        workspace_root=str(workspace_root or "").strip(),
        request_id=str(request_id or "").strip(),
    )
