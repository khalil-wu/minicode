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
from backend.atomic_io import atomic_write_bytes, file_mutation_locks
from backend.permissions.context import ToolExecutionContext
from backend.security.sensitive_files import is_protected_write_path
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema
from backend.tools.path_resolution import PathTraversalError, _is_bypass_mode, _resolve_path
from backend.workspace.file_state_cache import get_global_file_cache
from backend.workspace.path_filters import is_windows_reserved_path


from backend.tools.file_tools_common import (
    _atomic_write_text,
    _emit_write_diff,
    _generate_limited_unified_diff,
    _path_arg,
    _validate_expected_hash,
    _validate_path_arg_type,
    _validate_text_arg,
    _workspace_display_path,
    content_hash,
    invalidate_workspace_file_caches,
)

class WriteFileTool(BaseTool):
    """
    Write a complete text file.

    The harness injects a runtime-owned expected_hash guard for existing files.
    """

    name = "write_file"
    mutates_workspace = True
    result_kind = "edit"
    activity_kind = "fileChange"
    display_label = "Write"
    description = (
        "Create a new UTF-8 text file or replace a whole file. Use edit_file for small targeted changes. "
        "Read existing files first so the harness can inject their read-time guard. "
        "Do not create sibling output copies or unsolicited docs/README files."
    )
    permission = PermissionLevel.DIFF_REVIEW
    workspace_path_fields = ("file_path",)

    def check_permission(self, args=None, context=None):
        if context is not None and context.mode == "plan":
            from backend.agent.plans import is_current_plan_file

            return (
                PermissionLevel.AUTO
                if is_current_plan_file(_path_arg(args or {}), context)
                else PermissionLevel.ALWAYS_DENY
            )
        return None

    def is_capability_available(self, context=None) -> bool:
        return context is None or context.mode == "plan" or super().is_capability_available(context)

    def capability_permission_level(self, context=None):
        if context is not None and context.mode == "plan":
            return PermissionLevel.AUTO
        return self.permission

    def model_description(self) -> str:
        return (
            "Create or replace a whole UTF-8 text file. Read an existing file first."
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

    def streamed_input_preview(
        self,
        args: dict[str, Any],
        context: Any | None = None,
        prior: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        file_path = args.get("file_path")
        if not isinstance(file_path, str):
            return {}
        preview: dict[str, Any] = {"file_path": file_path}
        content = args.get("content")
        if not isinstance(content, str) or not content:
            return preview
        # A whole-file write replaces the target: the live badge can therefore
        # show real line counts while the content streams in. The pre-edit line
        # count is read once per call and cached in the private prior channel;
        # re-reading the workspace on every provider delta would be wasteful.
        prior_state = prior if isinstance(prior, dict) else {}
        baseline = prior_state.get("_baseline_lines")
        if baseline is None:
            if context is None:
                return preview
            try:
                resolved = _resolve_path(file_path, context)
            except PathTraversalError:
                return preview
            try:
                baseline = (
                    resolved.read_text(encoding="utf-8", errors="replace").count("\n") + 1
                    if resolved.is_file()
                    else 0
                )
            except OSError:
                return preview
        if not isinstance(baseline, int):
            return preview
        preview["diff"] = {"plus": content.count("\n") + 1, "minus": baseline}
        preview["_baseline_lines"] = baseline
        return preview

    def get_spec(self):
        from backend.tools.contracts import ToolSpec

        return ToolSpec(
            name=self.name,
            capability="workspace.write",
            required_args=("file_path", "content"),
        )

    def get_schema(self) -> ToolSchema:
        """Host-facing alias retained for direct callers; the model never sees it."""
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

    def get_execution_schema(self) -> ToolSchema:
        parameters = dict(self.model_schema().parameters)
        properties = dict(parameters.get("properties") or {})
        properties["expected_hash"] = {
            "type": "string",
            "description": "Runtime-owned read-time hash; injected by the harness.",
        }
        parameters["properties"] = properties
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters=parameters,
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
            path = _resolve_path(
                file_path,
                context,
                allow_workspace_escape=bypass_mode,
                allow_current_plan_file=True,
            )
        except PathTraversalError as exc:
            return self._error_result(str(exc))

        # Protected paths stay guarded even in bypass mode.
        if is_protected_write_path(path):
            return self._error_result(
                f"Refusing to write protected path: {file_path}. "
                "Repository and agent configuration files must be edited manually."
            )

        try:
            ok, message = _validate_expected_hash(path, args.get("expected_hash"))
            if not ok:
                return self._error_result(message)
            file_existed_before_write = path.exists()
            # Symlink + parent boundary check before write
            if path.exists() and path.is_symlink():
                from backend.agent.plans import is_current_plan_file

                if is_current_plan_file(path, context):
                    return self._error_result(f"Refusing to write through a plan-file symlink: {file_path}")
                if not bypass_mode:
                    real_target = path.resolve()
                    workspace_root = Path(context.workspace_root).resolve() if context and getattr(context, 'workspace_root', None) else Path.cwd().resolve()
                    try:
                        real_target.relative_to(workspace_root)
                    except ValueError:
                        return self._error_result(f"Refusing to write through symlink that escapes workspace: {file_path}")
            parent_resolved = path.parent.resolve()
            workspace_root = Path(context.workspace_root).resolve() if context and getattr(context, 'workspace_root', None) else Path.cwd().resolve()
            from backend.agent.plans import is_current_plan_file
            plan_file = is_current_plan_file(path, context)
            if not bypass_mode and not plan_file:
                try:
                    parent_resolved.relative_to(workspace_root)
                except ValueError:
                    return self._error_result(f"Parent directory escapes workspace boundary: {file_path}")
            # Validate the resolved parent before creating it. A failed boundary
            # check must not leave a new directory outside the workspace.
            path.parent.mkdir(parents=True, exist_ok=True)
            # Capture old content before overwriting (for diff in result).
            old_content = None
            if path.exists():
                try:
                    old_content = path.read_text(encoding="utf-8")
                except (UnicodeDecodeError, OSError):
                    pass

            # Repeat the guard inside the process-wide same-file queue. This
            # prevents two sessions that reviewed the same hash from both
            # committing, and also makes two simultaneous creates deterministic.
            with file_mutation_locks([path]):
                ok, message = _validate_expected_hash(path, args.get("expected_hash"))
                if not ok:
                    return self._error_result(message)
                file_existed_before_write = path.exists()
                if file_existed_before_write:
                    try:
                        old_content = path.read_text(encoding="utf-8")
                    except (UnicodeDecodeError, OSError):
                        old_content = None
                # Whole-file Write honors the exact line endings supplied by
                # the model. CC and Pi both distinguish this from Edit, which
                # preserves the existing file's line-ending style.
                atomic_write_bytes(path, content.encode("utf-8"))

                # Invalidate before releasing the queue so the next mutation
                # cannot consume stale model/editor state.
                cache = get_global_file_cache()
                cache.invalidate(path)
                invalidate_workspace_file_caches(file_tree_changed=not file_existed_before_write)
        except PermissionError:
            return self._error_result(f"No permission to write file: {file_path}")
        except OSError as exc:
            return self._error_result(
                f"Failed to write file ({type(exc).__name__}, errno={exc.errno})."
            )

        if not plan_file:
            await _emit_write_diff(
                context,
                file_path=file_path,
                old_content=old_content,
                new_content=content,
                display_path=_workspace_display_path(path, file_path, context),
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
