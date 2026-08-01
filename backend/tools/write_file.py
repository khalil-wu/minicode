"""WriteFileTool (extracted from file_tools.py)."""
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
from backend.security.sensitive_files import is_protected_write_path, is_sensitive_file
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema
from backend.tools.path_resolution import PathTraversalError, _is_bypass_mode, _resolve_path
from backend.workspace.file_state_cache import get_global_file_cache
from backend.workspace.path_filters import is_windows_reserved_path


from backend.tools.file_tools_common import *  # shared helpers (validation/diff/cache/etc.)

class WriteFileTool(BaseTool):
    """
    Write a complete text file.

    Existing files require an expected_hash guard before writing.
    """

    name = "write_file"
    mutates_workspace = True
    result_kind = "edit"
    activity_kind = "fileChange"
    display_label = "Write"
    description = (
        "Create a new UTF-8 text file or replace a whole file. Use edit_file for small targeted changes. "
        "Read existing files first and pass the latest content_hash as expected_hash. "
        "Do not create sibling output copies or unsolicited docs/README files."
    )
    permission = PermissionLevel.DIFF_REVIEW
    workspace_path_fields = ("file_path",)

    def model_description(self) -> str:
        return (
            "Create or replace a whole UTF-8 text file; use expected_hash for safe overwrite."
        )

    def model_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.model_description(),
            parameters={
                "type": "object",
                "properties": {
                    "file_path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["file_path", "content"],
            },
            strict=True,
        )

    def streamed_input_preview(self, args: dict[str, Any]) -> dict[str, Any]:
        file_path = args.get("file_path")
        return {"file_path": file_path} if isinstance(file_path, str) else {}

    def get_spec(self):
        from backend.tools.contracts import ToolSpec

        return ToolSpec(
            name=self.name,
            capability="workspace.write",
            required_args=("file_path", "content"),
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
                        "description": "Absolute or workspace-relative output path.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Complete UTF-8 file content.",
                    },
                    "expected_hash": {
                        "type": "string",
                        "description": "Latest content_hash for existing files; empty for new files.",
                    },
                },
                "required": ["file_path", "content"],
            },
            strict=True,
        )

    def validate_input(self, args: dict[str, Any] | None = None) -> str:
        args = args or {}
        return (
            _validate_path_arg_type(args)
            or _validate_text_arg(args, "content", role="the complete file contents")
        )

    async def execute(self, args: dict[str, Any], context: ToolExecutionContext | None = None) -> ToolResult:
        file_path = _path_arg(args)
        content = args.get("content", "")

        if not file_path:
            return self._error_result("Missing file_path argument")

        bypass_mode = _is_bypass_mode(context)
        try:
            path = _resolve_path(file_path, context, allow_workspace_escape=bypass_mode)
        except PathTraversalError as exc:
            return self._error_result(str(exc))

        if is_sensitive_file(path) and not bypass_mode:
            return self._error_result(
                f"Refusing to write sensitive file: {file_path}. "
                "Edit credential files manually outside the agent."
            )
        if is_protected_write_path(path) and not bypass_mode:
            return self._error_result(
                f"Refusing to write protected path: {file_path}. "
                "Repository and agent configuration files must be edited manually."
            )

        try:
            ok, message = _validate_expected_hash(path, args.get("expected_hash"))
            if not ok:
                return self._error_result(message)
            # Symlink + parent boundary check before write
            if path.exists() and path.is_symlink() and not bypass_mode:
                real_target = path.resolve()
                workspace_root = Path(context.workspace_root).resolve() if context and getattr(context, 'workspace_root', None) else Path.cwd().resolve()
                try:
                    real_target.relative_to(workspace_root)
                except ValueError:
                    return self._error_result(f"Refusing to write through symlink that escapes workspace: {file_path}")
            path.parent.mkdir(parents=True, exist_ok=True)
            parent_resolved = path.parent.resolve()
            workspace_root = Path(context.workspace_root).resolve() if context and getattr(context, 'workspace_root', None) else Path.cwd().resolve()
            if not bypass_mode:
                try:
                    parent_resolved.relative_to(workspace_root)
                except ValueError:
                    return self._error_result(f"Parent directory escapes workspace boundary: {file_path}")
            # Capture old content before overwriting (for diff in result).
            old_content = None
            if path.exists():
                try:
                    old_content = path.read_text(encoding="utf-8")
                except (UnicodeDecodeError, OSError):
                    pass

            _atomic_write_text(path, content)

            # Invalidate file caches after writing.
            cache = get_global_file_cache()
            cache.invalidate(path)
            clear_list_files_cache()
            await _emit_write_diff(
                context,
                file_path=file_path,
                old_content=old_content or "",
                new_content=content,
                display_path=_workspace_display_path(path, file_path, context),
            )
        except PermissionError:
            return self._error_result(f"No permission to write file: {file_path}")
        except OSError as exc:
            return self._error_result(
                f"Failed to write file ({type(exc).__name__}, errno={exc.errno})."
            )

        total_lines = len(content.split("\n"))
        result_msg = f"Wrote {file_path} ({total_lines} lines, {len(content)} chars). content_hash: {content_hash(content)}"
        if old_content is not None:
            _, additions, deletions, _ = _generate_limited_unified_diff(
                old_content,
                content,
                file_path,
                max_chars=0,
            )
            result_msg += f" Diff stats: +{additions} -{deletions}."
        return self._success_result(result_msg)

