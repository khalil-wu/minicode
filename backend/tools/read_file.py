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

from backend.artifact.store import ArtifactStore
from backend.permissions.context import ToolExecutionContext
from backend.agent.cache_metrics import args_signature, emit_cache_metric
from backend.security.sensitive_files import is_protected_write_path, is_sensitive_file
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema
from backend.tools.path_resolution import PathTraversalError, _is_bypass_mode, _resolve_path
from backend.workspace.file_state_cache import get_global_file_cache
from backend.workspace.path_filters import is_windows_reserved_path


from backend.tools.file_tools_common import *  # shared helpers (validation/diff/cache/etc.)

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
    panel_hint = "inspector"
    # Self-bounds via READ_FILE_TOKEN_LIMIT and artifacts large files; the global
    # backstop would only re-truncate an already-compact preview.
    max_result_chars = None
    description = (
        "Read a text file from the workspace. Returns content with line numbers and a content_hash for safe edits. "
        "Use before modifying files. Supports optional line ranges; large files are saved as artifacts."
    )
    permission = PermissionLevel.AUTO

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
                    "file_path": {"type": "string"},
                    "start_line": {"type": "integer"},
                    "end_line": {"type": "integer"},
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
            arg_roles={"file_path": "workspace_file", "path": "workspace_file"},
            arg_sources={"file_path": ("recent_list_files", "workspace_context", "primary_file")},
            repair_policy={"file_path": "resource_resolver"},
            accepted_resource_types=("workspace_file",),
            empty_args_policy="repair_or_ask",
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
                        "description": "Optional 1-indexed start line.",
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
            path = _resolve_path(file_path, context, allow_workspace_escape=_is_bypass_mode(context))
        except PathTraversalError as exc:
            return self._error_result(str(exc))

        if not path.exists():
            return self._error_result(f"File does not exist: {file_path}")

        if not path.is_file():
            return self._error_result(f"Not a file: {file_path}")

        if is_sensitive_file(path) and not _is_bypass_mode(context):
            return self._error_result(
                f"Refusing to read sensitive file: {file_path}. "
                "Open it manually or provide a redacted excerpt if it is needed."
            )
        # Refuse very large direct reads to avoid memory pressure.
        try:
            file_size = path.stat().st_size
        except OSError as exc:
            return self._error_result(f"Unable to read file metadata: {exc}")
        if file_size > MAX_FILE_READ_BYTES and not has_line_range:
            return self._error_result(
                f"File is too large ({file_size // 1024 // 1024}MB); limit is {MAX_FILE_READ_BYTES // 1024 // 1024}MB"
            )

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

        # Store oversized content as an artifact while returning a usable preview.
        estimated_tokens = len(content) // 4
        file_hash = content_hash(content)

        # FILE_UNCHANGED_STUB: if this file was already read with the same hash
        # in this conversation, return a stub instead of the full content (ClaudeCode pattern).
        if context is not None and not has_line_range:
            seen = context.metadata.setdefault("_read_file_hashes", {})
            path_key = str(path)
            if seen.get(path_key) == file_hash:
                return ToolResult(
                    content=f"File unchanged since last read. The content_hash {file_hash} is still current — refer to the earlier read_file result instead of re-reading.",
                    extraction_status="ok",
                )
            seen[path_key] = file_hash

        if estimated_tokens <= READ_FILE_TOKEN_LIMIT:
            content = _add_line_numbers(content, start_line=(line_offset + 1) if line_offset else 1)
            hash_label = "range_hash" if has_line_range else "content_hash"
            range_note = "\n[range only; read the full file to obtain a write-safe content_hash]" if has_line_range else ""
            return self._success_result(f"{content}\n\n[{hash_label}: {file_hash}]{range_note}")

        # Large files are saved as artifacts; the preview remains actionable.
        # The artifact can be opened later with read_artifact if needed.
        artifact_id = self._artifact_store.save(
            content=content,
            source=f"read_file({file_path})",
            type="code" if path.suffix in ('.py', '.js', '.ts', '.tsx', '.jsx', '.go', '.rs') else "text",
        )
        total_lines = len(content.split("\n"))
        preview = content[:READ_FILE_CONTEXT_PREVIEW_CHARS]
        if len(content) > READ_FILE_CONTEXT_PREVIEW_CHARS:
            preview += (
                f"\n... [truncated {len(content) - READ_FILE_CONTEXT_PREVIEW_CHARS} chars; "
                f"use read_artifact('{artifact_id}') only if the omitted tail is needed] ..."
            )
        preview = _add_line_numbers(preview, start_line=(line_offset + 1) if line_offset else 1)

        hash_label = "range_hash" if has_line_range else "content_hash"
        range_note = "\nrange only; read the full file to obtain a write-safe content_hash." if has_line_range else ""
        return self._success_result(
            content=f"File {file_path} ({total_lines} lines, approx {estimated_tokens} tokens) was saved as an artifact.\n{hash_label}: {file_hash}{range_note}",
            artifact_id=artifact_id,
            artifact_preview=preview,
        )

