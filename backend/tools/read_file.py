"""ReadFileTool (extracted from file_tools.py)."""
from __future__ import annotations

import difflib
import asyncio
import os
import tempfile
import time
import hashlib
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any

from backend.atomic_io import canonical_file_path_key, canonical_path_mapping_key
from backend.artifact.store import ArtifactStore
from backend.permissions.context import ToolExecutionContext
from backend.agent.cache_metrics import args_signature, emit_cache_metric
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema
from backend.tools.path_resolution import PathTraversalError, _is_bypass_mode, _resolve_path
from backend.workspace.file_state_cache import get_global_file_cache
from backend.workspace.path_filters import is_windows_reserved_path


from backend.tools.file_tools_common import (
    MAX_FILE_READ_BYTES,
    READ_FILE_MAX_BYTES,
    READ_FILE_MAX_LINES,
    _add_line_numbers,
    _coerce_line_number,
    _path_arg,
    _read_text_range,
    content_hash,
)


def _file_version(path: Path, stat_result: os.stat_result) -> str:
    return f"{canonical_file_path_key(path)}:{stat_result.st_size}:{stat_result.st_mtime_ns}"


def _range_already_returned(
    context: ToolExecutionContext | None,
    *,
    path: Path,
    version: str,
    start_line: int,
    end_line: int,
    record: bool,
) -> bool:
    if context is None or end_line < start_line:
        return False
    states = context.metadata.setdefault("_read_file_ranges", {})
    path_key = canonical_path_mapping_key(states, path)
    state = states.get(path_key)
    if not isinstance(state, dict) or state.get("version") != version:
        state = {"version": version, "ranges": []}
        states[path_key] = state
    ranges = [
        (int(item[0]), int(item[1]))
        for item in state.get("ranges", [])
        if isinstance(item, (list, tuple)) and len(item) == 2
    ]
    covered = any(start_line >= lower and end_line <= upper for lower, upper in ranges)
    if covered or not record:
        return covered

    ranges.append((start_line, end_line))
    merged: list[list[int]] = []
    for lower, upper in sorted(ranges):
        if merged and lower <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], upper)
        else:
            merged.append([lower, upper])
    state["ranges"] = merged
    return False

class ReadFileTool(BaseTool):
    """
    Read text file content.

    Small files are returned inline. Large files are saved as artifacts while
    still returning a usable preview and content hash.
    """

    name = "read_file"
    read_only = True
    result_kind = "file"
    activity_kind = "fileRead"
    display_label = "Read"
    # Self-bounds via the 2000-line/50-KiB contract and artifacts large files;
    # the global backstop would only re-truncate an already-compact preview.
    max_result_chars = None
    description = (
        "Read a text file from the workspace. Returns content with line numbers and a content_hash for safe edits. "
        "Use before modifying files. Supports optional line ranges; focused reads also return a current full-file "
        "content_hash when it can be computed, so a narrow read can be followed by a guarded edit. Large files are "
        "saved as artifacts."
    )
    permission = PermissionLevel.AUTO
    workspace_path_fields = ("file_path",)
    allow_workspace_root_path = True
    allow_tool_result_path = True

    def __init__(self, artifact_store: ArtifactStore) -> None:
        self._artifact_store = artifact_store

    def model_description(self) -> str:
        return "Read a text file with line numbers and content_hash."

    def model_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.model_description(),
            parameters={
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Workspace file path."},
                    "start_line": {
                        "type": "integer",
                        "description": "Optional first line for a focused range. A focused read still returns a current full-file content_hash when available.",
                    },
                    "end_line": {"type": "integer", "description": "Optional inclusive last line."},
                },
                "required": ["file_path"],
            },
            strict=True,
        )

    def get_spec(self):
        from backend.tools.contracts import ToolSpec

        return ToolSpec(
            name=self.name,
            capability="workspace.read",
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
                        "description": "Absolute or workspace-relative file path.",
                    },
                    "start_line": {
                        "type": "integer",
                        "description": "Optional 1-indexed start line. Focused reads include a current full-file content_hash when available.",
                    },
                    "end_line": {
                        "type": "integer",
                        "description": "Optional 1-indexed inclusive end line.",
                    },
                },
                "required": ["file_path"],
            },
            strict=True,
        )

    def _read_pdf_text(self, path: Path, file_path: str, file_size: int) -> ToolResult:
        """Extract text from a PDF via PyMuPDF.

        Page text is capped at a fixed page budget. PDFs aren't editable via
        edit_file, so no content_hash for editing is included.
        """
        try:
            import fitz  # PyMuPDF
        except ImportError:
            return self._error_result(
                f"Cannot read PDF {file_path}: PyMuPDF (fitz) is not installed."
            )
        try:
            doc = fitz.open(path)
        except Exception as exc:
            return self._error_result(f"Failed to open PDF {file_path}: {exc}")
        try:
            page_count = doc.page_count
            max_pages = min(page_count, 20)
            pages: list[str] = []
            for index in range(max_pages):
                text = (doc.load_page(index).get_text("text") or "").rstrip()
                pages.append(f"--- Page {index + 1} ---\n{text}")
            doc.close()
        except Exception as exc:
            doc.close()
            return self._error_result(f"Failed to extract PDF text: {exc}")
        body = "\n\n".join(pages)
        if page_count > max_pages:
            body += f"\n\n... ({page_count - max_pages} more page(s); showing first {max_pages})"
        return self._success_result(
            f"PDF {file_path} ({page_count} page(s), {file_size // 1024}KB):\n\n{body}"
        )

    def _read_image(self, path: Path, file_path: str, file_size: int) -> ToolResult:
        """Return an image as a base64 tool_result image block.

        An image content block lets the model actually see the image; the
        adapter renders ToolResult.images as provider image blocks.
        """
        ext = path.suffix.lower()
        media_type = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".gif": "image/gif",
            ".webp": "image/webp",
        }.get(ext, "image/png")
        try:
            raw = path.read_bytes()
        except OSError as exc:
            return self._error_result(f"Failed to read image {file_path}: {exc}")
        import base64

        return ToolResult(
            content=f"Image {file_path} ({ext.lstrip('.')}, {file_size // 1024}KB).",
            images=[
                {
                    "media_type": media_type,
                    "data": base64.b64encode(raw).decode("ascii"),
                }
            ],
        )

    async def execute(self, args: dict[str, Any], context: ToolExecutionContext | None = None) -> ToolResult:
        file_path = _path_arg(args)
        start_line_arg = args.get("start_line")
        end_line_arg = args.get("end_line")
        has_line_range = start_line_arg is not None or end_line_arg is not None
        start_line = _coerce_line_number(start_line_arg, default=1)
        end_line = _coerce_line_number(end_line_arg)

        if not file_path:
            return self._error_result("Missing file_path argument")

        try:
            path = _resolve_path(
                file_path,
                context,
                allow_workspace_escape=_is_bypass_mode(context),
                allow_tool_result_root=True,
                allow_declared_read_root=True,
                allow_current_plan_file=True,
            )
        except PathTraversalError as exc:
            return self._error_result(str(exc))

        if not path.exists():
            return self._error_result(f"File does not exist: {file_path}")

        if not path.is_file():
            return self._error_result(f"Not a file: {file_path}")

        # Refuse very large direct reads to avoid memory pressure.
        try:
            stat_result = path.stat()
            file_size = stat_result.st_size
        except OSError as exc:
            return self._error_result(f"Unable to read file metadata: {exc}")
        if file_size > MAX_FILE_READ_BYTES and not has_line_range:
            return self._error_result(
                f"File is too large ({file_size // 1024 // 1024}MB); limit is {MAX_FILE_READ_BYTES // 1024 // 1024}MB"
            )

        # PDF: extract text via PyMuPDF. Binary PDFs cannot be read as UTF-8,
        # so without this read_file errors out.
        if path.suffix.lower() == ".pdf":
            return self._read_pdf_text(path, file_path, file_size)

        if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
            return self._read_image(path, file_path, file_size)

        line_offset = 0
        if has_line_range:
            if end_line is not None and end_line < start_line:
                return self._error_result("end_line must be greater than or equal to start_line")
            try:
                content = _read_text_range(
                    path,
                    start_line=start_line,
                    end_line=end_line,
                    max_bytes=MAX_FILE_READ_BYTES,
                )
                line_offset = start_line - 1
            except UnicodeDecodeError:
                return self._error_result(
                    f"Cannot read binary or non-UTF-8 file: {file_path}. "
                    "This tool only supports UTF-8 text files."
                )
            except PermissionError:
                return self._error_result(f"No permission to read file: {file_path}")
            except ValueError as exc:
                return self._error_result(str(exc))
            except OSError as exc:
                return self._error_result(f"Failed to read file: {exc}")
        else:
            # Try the file-state cache first for complete reads.
            cache = get_global_file_cache()
            cached_entry = cache.get(path)
            signature = args_signature({"file_path": str(path)})

            if cached_entry is not None:
                content = cached_entry.content
                await emit_cache_metric(
                    context,
                    cache_layer="read_file.file_state",
                    tool_name=self.name,
                    args_signature_value=signature,
                    hit=True,
                    payload_size_bytes=len(content.encode("utf-8")),
                )
            else:
                try:
                    content = path.read_text(encoding="utf-8")
                    # Cache file content for subsequent reads.
                    language_hint = path.suffix.lstrip(".") if path.suffix else ""
                    cache.put(path, content, language_hint)
                    await emit_cache_metric(
                        context,
                        cache_layer="read_file.file_state",
                        tool_name=self.name,
                        args_signature_value=signature,
                        hit=False,
                        payload_size_bytes=len(content.encode("utf-8")),
                    )
                except UnicodeDecodeError:
                    return self._error_result(
                        f"Cannot read binary or non-UTF-8 file: {file_path}. "
                        "This tool only supports UTF-8 text files."
                    )
                except PermissionError:
                    return self._error_result(f"No permission to read file: {file_path}")
                except OSError as exc:
                    return self._error_result(f"Failed to read file: {exc}")

        # Apply MiniCode's bounded line/UTF-8-byte contract before display numbering.
        file_hash = content_hash(content)
        returned_line_count = len(content.splitlines())
        returned_start_line = (line_offset + 1) if line_offset else 1
        returned_end_line = returned_start_line + returned_line_count - 1
        version = _file_version(path, stat_result)

        # A focused read is normally the right way to inspect a large source
        # file, but writes still need an optimistic full-file hash.  Compute it
        # privately (without returning the omitted content) so the model can
        # read a small range and immediately issue a guarded edit.  Reuse the
        # file-state cache when possible; files above the direct-read limit stay
        # range-only because loading their complete text would defeat the
        # bounded-read contract.
        write_safe_hash = ""
        if has_line_range and file_size <= MAX_FILE_READ_BYTES:
            try:
                cache = get_global_file_cache()
                cached_full = cache.get(path)
                if cached_full is not None:
                    full_content = cached_full.content
                else:
                    full_content = path.read_text(encoding="utf-8")
                    language_hint = path.suffix.lstrip(".") if path.suffix else ""
                    cache.put(path, full_content, language_hint)
                write_safe_hash = content_hash(full_content)
            except (UnicodeDecodeError, OSError):
                # The requested range remains useful even when a full-file
                # hash cannot be produced (for example, a concurrently
                # changing or oversized/non-text file).
                write_safe_hash = ""

        if context is not None and write_safe_hash:
            seen = context.metadata.setdefault("_read_file_hashes", {})
            path_key = canonical_path_mapping_key(seen, path)
            seen.pop(path_key, None)
            seen[path_key] = write_safe_hash

        # Keep the latest full-file hash for optimistic edit guards. Do not
        # replace an explicit read with an "already returned" stub: context
        # compaction may have removed the earlier tool result, and a subagent
        # then has no way to recover the evidence it is asking for. The file
        # state cache already avoids repeat disk I/O.
        if context is not None and not has_line_range:
            seen = context.metadata.setdefault("_read_file_hashes", {})
            path_key = canonical_path_mapping_key(seen, path)
            seen.pop(path_key, None)
            seen[path_key] = file_hash

        _range_already_returned(
            context,
            path=path,
            version=version,
            start_line=returned_start_line,
            end_line=returned_end_line,
            record=True,
        )
        numbered = _add_line_numbers(content, start_line=(line_offset + 1) if line_offset else 1)
        from backend.tools.base import truncate_text_head

        truncation = truncate_text_head(
            numbered,
            max_lines=READ_FILE_MAX_LINES,
            max_bytes=READ_FILE_MAX_BYTES,
        )
        hash_label = "range_hash" if has_line_range else "content_hash"
        hash_lines = [f"[{hash_label}: {file_hash}]"]
        if has_line_range and write_safe_hash:
            hash_lines.append(f"[content_hash: {write_safe_hash}; write-safe full-file version]")
        elif has_line_range:
            hash_lines.append("[range only; a write-safe full-file content_hash was unavailable]")

        if not truncation.truncated:
            return self._success_result(f"{numbered}\n\n" + "\n".join(hash_lines))

        # Preserve the complete file for explicit artifact reads while making
        # the inline result resumable with start_line/end_line.
        artifact_id = self._artifact_store.save(
            content=content,
            source=f"read_file({file_path})",
            type="code" if path.suffix in ('.py', '.js', '.ts', '.tsx', '.jsx', '.go', '.rs') else "text",
        )
        if truncation.first_line_exceeds_limit:
            preview = (
                f"[Line {returned_start_line} exceeds {READ_FILE_MAX_BYTES} byte limit. "
                f"Use read_artifact('{artifact_id}') or a focused start_line/end_line read.]"
            )
        else:
            preview = truncation.content
            end_line = returned_start_line + truncation.output_lines - 1
            next_offset = end_line + 1
            if truncation.truncated_by == "lines":
                preview += (
                    f"\n\n[Showing lines {returned_start_line}-{end_line} of {returned_end_line}. "
                    f"Use start_line={next_offset} to continue.]"
                )
            else:
                preview += (
                    f"\n\n[Showing lines {returned_start_line}-{end_line} of {returned_end_line} "
                    f"({READ_FILE_MAX_BYTES} byte limit). Use start_line={next_offset} to continue.]"
                )
        preview += f"\n\n[Full content available via read_artifact('{artifact_id}').]"
        return self._success_result(
            content=f"{preview}\n\n" + "\n".join(hash_lines),
            artifact_id=artifact_id,
            artifact_preview=preview,
        )

    def streamed_input_preview(
        self,
        args: dict[str, Any],
        context: Any | None = None,
        prior: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            key: args[key]
            for key in ("file_path", "start_line", "end_line")
            if key in args and isinstance(args[key], (str, int))
        }
