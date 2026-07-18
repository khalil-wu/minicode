from __future__ import annotations

from typing import Any

from backend.artifact.store import ArtifactStore
from backend.attachments.store import AttachmentStore
from backend.permissions.context import ToolExecutionContext
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema


def artifact_content_preview(content: str, *, max_chars: int = 1600) -> str:
    text = str(content or "").strip()
    if len(text) <= max_chars:
        return text
    head = text[: max_chars - 80].rstrip()
    return f"{head}\n... [{len(text) - len(head)} chars omitted; expand/open artifact for full content] ..."


class ReadArtifactTool(BaseTool):
    """Read full content from an artifact created by a previous tool."""

    name = "read_artifact"
    read_only = True
    result_kind = "file"
    activity_kind = "fileRead"
    display_label = "Read"
    panel_hint = "inspector"
    description = (
        "Read complete artifact content when a previous tool returned an artifact_id. "
        "Only use artifact IDs that appeared in this conversation."
    )
    permission = PermissionLevel.AUTO

    def model_description(self) -> str:
        return "Read full artifact content by artifact_id."

    def __init__(
        self,
        artifact_store: ArtifactStore,
        *,
        attachment_store: AttachmentStore | None = None,
    ) -> None:
        self._artifact_store = artifact_store
        self._attachment_store = attachment_store

    def get_spec(self):
        from backend.tools.contracts import ToolSpec

        return ToolSpec(
            name=self.name,
            capability="artifact.read",
            exposure="deferred",
            required_args=("artifact_id",),
            arg_roles={"artifact_id": "latest_artifact"},
            empty_args_policy="repair_or_block",
        )

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "artifact_id": {
                        "type": "string",
                        "description": "Artifact identifier, for example 'art_a1b2c3d4'.",
                    },
                },
                "required": ["artifact_id"],
            },
            strict=True,
        )

    def model_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.model_description(),
            parameters={
                "type": "object",
                "properties": {
                    "artifact_id": {"type": "string"},
                },
                "required": ["artifact_id"],
            },
            strict=True,
        )

    async def execute(self, args: dict[str, Any], context: ToolExecutionContext | None = None) -> ToolResult:
        artifact_id = args.get("artifact_id", "")
        if not artifact_id:
            return self._error_result("Missing artifact_id argument")

        content = self._artifact_store.get(artifact_id)
        if content is None and self._attachment_store is not None:
            content = self._attachment_store.get(artifact_id)
        if content is None and self._attachment_store is not None:
            resolved = self._attachment_store.resolve_content(artifact_id)
            if resolved is not None:
                _resolved_artifact_id, content, _metadata = resolved

        if content and self._is_parse_error(content) and self._attachment_store is not None:
            reparsed = self._try_reparse(artifact_id)
            if reparsed:
                content = reparsed

        if content is None:
            available = self._artifact_store.list_artifacts()
            ids = [a.artifact_id for a in available]
            hint = f"Available artifacts: {', '.join(ids)}" if ids else "No artifacts are currently available"
            return self._error_result(
                f"Artifact '{artifact_id}' does not exist. {hint}"
            )

        preview = artifact_content_preview(content)
        return ToolResult(
            content=content,
            content_preview=preview,
            display_summary=f"Read artifact {artifact_id}",
        )

    @staticmethod
    def _is_parse_error(content: str) -> bool:
        text = (content or "").strip().lower()
        return text.startswith(("错误:", "error:", "parse error:", "pdf parse failed"))

    def _try_reparse(self, artifact_id: str) -> str | None:
        import base64
        import os
        import tempfile

        if self._attachment_store is None:
            return None
        payload = self._attachment_store.find_payload(artifact_id)
        if not payload:
            return None
        metadata = payload.get("metadata")
        if not isinstance(metadata, dict):
            return None
        attachment = metadata.get("attachment")
        if not isinstance(attachment, dict):
            return None
        native_data = attachment.get("data", "")
        file_name = attachment.get("file_name", "")
        if not native_data or not file_name:
            return None

        suffix = "." + file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""
        if suffix != ".pdf":
            return None

        try:
            raw_bytes = base64.b64decode(native_data)
        except Exception:
            return None

        fd, temp_path = tempfile.mkstemp(suffix=suffix)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(raw_bytes)
            from backend.mcp.servers.docparse import _parse_pdf

            parsed = _parse_pdf(temp_path)
            full_text = str(parsed.get("full_text", "")).strip()
            if not full_text or self._is_parse_error(full_text):
                return None
            self._attachment_store.save(
                artifact_id=artifact_id,
                content=full_text,
                metadata=metadata,
            )
            from backend.artifact.store import ARTIFACT_DATA_DIR

            content_path = ARTIFACT_DATA_DIR / f"{artifact_id}.txt"
            if content_path.parent.exists():
                content_path.write_text(full_text, encoding="utf-8")
            return full_text
        except Exception:
            return None
        finally:
            try:
                os.remove(temp_path)
            except OSError:
                pass
