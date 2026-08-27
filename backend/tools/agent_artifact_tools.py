from __future__ import annotations

import mimetypes
import os
from pathlib import Path
from typing import Any

from backend.artifact.store import ArtifactStore
from backend.atomic_io import file_mutation_locks
from backend.attachments.store import AttachmentStore
from backend.permissions.context import ToolExecutionContext
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema
from backend.tools.file_tools_common import _atomic_write_text


PRESENTABLE_FILE_EXTENSIONS = frozenset({
    ".7z", ".aac", ".avif", ".bmp", ".csv", ".doc", ".docx", ".epub",
    ".flac", ".gif", ".htm", ".html", ".ico", ".jpeg", ".jpg", ".json",
    ".m4a", ".md", ".mov", ".mp3", ".mp4", ".odp", ".ods", ".odt", ".ogg",
    ".pdf", ".png", ".ppt", ".pptx", ".rtf", ".svg", ".tar", ".tif",
    ".tiff", ".tsv", ".txt", ".wav", ".webm", ".webp", ".xls", ".xlsx",
    ".xml", ".yaml", ".yml", ".zip",
})


def _known_output_roots(context: ToolExecutionContext | None) -> list[Path]:
    roots: list[Path] = []
    if context is not None and context.workspace_root:
        roots.append(Path(context.workspace_root).resolve())
    for name in ("MINICODE_DESKTOP_DIR", "MINICODE_DOCUMENTS_DIR", "MINICODE_DOWNLOADS_DIR"):
        value = str(os.environ.get(name) or "").strip()
        if value:
            roots.append(Path(value).resolve())
    return roots


def _is_within(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath([os.path.normcase(str(path)), os.path.normcase(str(root))]) == os.path.normcase(str(root))
    except ValueError:
        return False


class PresentFileTool(BaseTool):
    """Validate and register a generated user-facing file for final delivery."""

    name = "present_file"
    read_only = True
    result_kind = "generic"
    activity_kind = "genericTool"
    display_label = "Present file"
    description = (
        "Register a completed user-facing document or media deliverable after verifying it exists. "
        "Call this for final artifacts such as DOCX, PDF, XLSX, PPTX, images, self-contained HTML "
        "pages, text, or archives; "
        "never call it for temporary helper scripts, source code, or intermediate build files."
    )
    permission = PermissionLevel.AUTO
    workspace_path_fields = ("file_path",)

    def get_spec(self):
        from backend.tools.contracts import ToolSpec

        return ToolSpec(
            name=self.name,
            capability="artifact.present",
            toolset="artifact",
            exposure="core",
            required_args=("file_path",),
        )

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Absolute path to the final generated deliverable.",
                    },
                    "label": {
                        "type": "string",
                        "description": "Optional concise display name; defaults to the file name.",
                    },
                },
                "required": ["file_path"],
            },
            strict=True,
        )

    async def execute(self, args: dict[str, Any], context: ToolExecutionContext | None = None) -> ToolResult:
        raw_path = str(args.get("file_path") or "").strip()
        if not raw_path:
            return self._error_result("Missing file_path argument")
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            if context is None or not context.workspace_root:
                return self._error_result("file_path must be absolute when no workspace is active")
            candidate = Path(context.workspace_root) / candidate
        resolved = candidate.resolve()
        if not any(_is_within(resolved, root) for root in _known_output_roots(context)):
            return ToolResult(
                content=(
                    "The deliverable is outside the active workspace and the host-resolved "
                    "Desktop/Documents/Downloads folders. Move it to an approved output folder first."
                ),
                is_error=True,
                status="blocked",
                display_summary="Deliverable path is not approved",
                result_kind="generic",
            )
        if resolved.suffix.lower() not in PRESENTABLE_FILE_EXTENSIONS:
            return ToolResult(
                content=(
                    f"'{resolved.name}' is not a supported user-facing deliverable type. "
                    "Do not present source files or executable scripts."
                ),
                is_error=True,
                status="blocked",
                display_summary="Unsupported deliverable type",
                result_kind="generic",
            )
        try:
            stat = resolved.stat()
        except OSError as exc:
            return self._error_result(f"Deliverable does not exist: {resolved} ({exc})")
        if not resolved.is_file():
            return self._error_result(f"Deliverable is not a regular file: {resolved}")

        label = str(args.get("label") or resolved.name).strip() or resolved.name
        markdown_path = resolved.as_posix()
        mime_type = mimetypes.guess_type(resolved.name)[0] or "application/octet-stream"
        output_file = {
            "path": str(resolved),
            "name": label,
            "size": int(stat.st_size),
            "mime_type": mime_type,
            "is_image": mime_type.startswith("image/"),
        }
        return ToolResult(
            content=f"Verified deliverable: [{label}](<{markdown_path}>)",
            display_summary=f"Ready: {label}",
            result_kind="generic",
            output_files=[output_file],
        )


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
    description = (
        "Read complete artifact content when a previous tool returned an artifact_id. "
        "It also accepts a bare MiniCode persisted-result cache filename shown by a tool. "
        "Only use references that appeared in this conversation."
    )
    permission = PermissionLevel.AUTO

    def model_description(self) -> str:
        return "Read full content by artifact_id or a shown MiniCode persisted-result cache filename."

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
            toolset="artifact",
            exposure="core",
            required_args=("artifact_id",),
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
                        "description": (
                            "Artifact identifier such as 'art_a1b2c3d4', or a bare persisted "
                            "MiniCode result filename explicitly shown by a tool."
                        ),
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Optional 1-indexed first line to return. Use to page through an artifact larger than one result.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Optional number of lines to return starting at offset.",
                    },
                },
                "required": ["artifact_id"],
            },
        )

    def model_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.model_description(),
            parameters={
                "type": "object",
                "properties": {
                    "artifact_id": {"type": "string"},
                    "offset": {
                        "type": "integer",
                        "description": "Optional 1-indexed first line; page through artifacts too large for one result.",
                    },
                    "limit": {"type": "integer", "description": "Optional line count from offset."},
                },
                "required": ["artifact_id"],
            },
        )

    async def execute(self, args: dict[str, Any], context: ToolExecutionContext | None = None) -> ToolResult:
        artifact_id = str(args.get("artifact_id", "") or "").strip()
        if not artifact_id:
            return self._error_result("Missing artifact_id argument")

        conversation_id = str(getattr(context, "conversation_id", "") or "") if context else ""
        workspace_root = str(getattr(context, "workspace_root", "") or "") if context else ""
        content = self._artifact_store.get(
            artifact_id,
            conversation_id=conversation_id,
            workspace_root=workspace_root,
        )
        if content is None and self._attachment_store is not None:
            content = self._attachment_store.get(
                artifact_id,
                conversation_id=conversation_id,
                workspace_root=workspace_root,
            )
        if content is None and self._attachment_store is not None:
            resolved = self._attachment_store.resolve_content(
                artifact_id,
                conversation_id=conversation_id,
                workspace_root=workspace_root,
            )
            if resolved is not None:
                _resolved_artifact_id, content, _metadata = resolved

        if content and self._is_parse_error(content) and self._attachment_store is not None:
            reparsed = self._try_reparse(
                artifact_id,
                conversation_id=conversation_id,
                workspace_root=workspace_root,
            )
            if reparsed:
                content = reparsed

        # Large tool outputs predate the ArtifactStore path and are persisted in
        # MiniCode's read-only tool-result cache. Models sometimes pass the
        # explicitly shown cache filename to read_artifact; resolve only a bare
        # filename inside that trusted directory so the two result-reference
        # mechanisms interoperate without opening an arbitrary-file read path.
        if content is None:
            content = self._read_persisted_tool_result(
                artifact_id,
                conversation_id=conversation_id,
                workspace_root=workspace_root,
            )

        if content is None:
            return self._error_result(
                f"Artifact '{artifact_id}' does not exist in this conversation and workspace."
            )

        sliced, window = self._slice_lines(content, args)
        # A preview of the artifact head would be appended to the model-visible
        # result alongside the slice, re-adding the very content offset/limit
        # was used to skip. Only an unpaged read gets the head preview.
        preview = artifact_content_preview(content) if not window else None
        return ToolResult(
            content=sliced,
            content_preview=preview,
            display_summary=f"Read artifact {artifact_id}{window}",
        )

    @staticmethod
    def _slice_lines(content: str, args: dict[str, Any]) -> tuple[str, str]:
        """Return the requested line window, plus a label for the UI summary."""

        def _positive_int(key: str) -> int | None:
            raw = args.get(key)
            if raw is None or isinstance(raw, bool):
                return None
            try:
                value = int(raw)
            except (TypeError, ValueError):
                return None
            return value if value > 0 else None

        offset = _positive_int("offset")
        limit = _positive_int("limit")
        if offset is None and limit is None:
            return content, ""

        lines = content.splitlines()
        start = (offset - 1) if offset else 0
        if start >= len(lines):
            return (
                f"[artifact has {len(lines)} lines; offset {offset} is past the end]",
                f" (offset {offset} past end)",
            )
        end = min(len(lines), start + limit) if limit else len(lines)
        body = "\n".join(lines[start:end])
        header = f"[artifact lines {start + 1}-{end} of {len(lines)}]"
        return f"{header}\n{body}", f" lines {start + 1}-{end}"

    @staticmethod
    def _read_persisted_tool_result(
        reference: str,
        *,
        conversation_id: str = "",
        workspace_root: str = "",
    ) -> str | None:
        from pathlib import Path

        from backend.agent.tool_result_persistence import (
            TOOL_RESULT_DATA_DIR,
            is_tool_result_path,
        )

        raw = Path(reference)
        if raw.name != reference or raw.name in {"", ".", ".."}:
            return None
        candidate = TOOL_RESULT_DATA_DIR / raw.name
        if not is_tool_result_path(
            candidate,
            conversation_id=conversation_id,
            workspace_root=workspace_root,
        ):
            return None
        try:
            return candidate.read_text(encoding="utf-8")
        except OSError:
            return None

    @staticmethod
    def _is_parse_error(content: str) -> bool:
        text = (content or "").strip().lower()
        return text.startswith(("错误:", "error:", "parse error:", "pdf parse failed"))

    def _try_reparse(
        self,
        artifact_id: str,
        *,
        conversation_id: str = "",
        workspace_root: str = "",
    ) -> str | None:
        import base64
        import os
        import tempfile

        if self._attachment_store is None:
            return None
        payload = self._attachment_store.find_payload(
            artifact_id,
            conversation_id=conversation_id,
            workspace_root=workspace_root,
        )
        if not payload:
            return None
        metadata = payload.get("metadata")
        if not isinstance(metadata, dict):
            return None
        attachment = metadata.get("attachment")
        if not isinstance(attachment, dict):
            return None
        native_data = str(
            attachment.get("data", "") or payload.get("native_data") or ""
        )
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
            from backend.documents.parsers import _parse_pdf

            parsed = _parse_pdf(temp_path)
            full_text = str(parsed.get("full_text", "")).strip()
            if not full_text or self._is_parse_error(full_text):
                return None
            self._attachment_store.save(
                artifact_id=artifact_id,
                content=full_text,
                metadata=metadata,
                native_data=native_data,
            )
            from backend.artifact.store import ARTIFACT_DATA_DIR

            content_path = ARTIFACT_DATA_DIR / f"{artifact_id}.txt"
            with file_mutation_locks([content_path]):
                if content_path.parent.exists():
                    _atomic_write_text(content_path, full_text)
            return full_text
        except Exception:
            return None
        finally:
            try:
                os.remove(temp_path)
            except OSError:
                pass
