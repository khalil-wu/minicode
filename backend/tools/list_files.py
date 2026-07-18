"""ListFilesTool (extracted from file_tools.py)."""
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

class ListFilesTool(BaseTool):
    """
    List directory contents.

    Returns at most LIST_FILES_MAX_ENTRIES entries.
    """

    name = "list_files"
    read_only = True
    result_kind = "file"
    activity_kind = "workspaceSearch"
    display_label = "List"
    panel_hint = "inspector"
    description = (
        "List files and directories with sizes/types. Use to explore project structure; use glob_files for name patterns and grep_files for content."
    )
    permission = PermissionLevel.AUTO

    def model_description(self) -> str:
        return "List files and directories to inspect project structure."

    def model_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.model_description(),
            parameters={
                "type": "object",
                "properties": {
                    "directory": {"type": "string"},
                    "recursive": {"type": "boolean"},
                },
                "required": [],
            },
        )

    def get_spec(self):
        from backend.tools.contracts import ToolSpec

        return ToolSpec(
            name=self.name,
            capability="workspace.list",
            exposure="deferred",
            default_args={"directory": "."},
            accepted_resource_types=("workspace_file", "workspace_directory"),
            empty_args_policy="repair_or_block",
        )

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "directory": {
                        "type": "string",
                        "description": "Absolute or workspace-relative directory path.",
                    },
                    "recursive": {
                        "type": "boolean",
                        "description": "List recursively. Default false.",
                    },
                },
                "required": [],
            },
        )

    async def execute(self, args: dict[str, Any], context: ToolExecutionContext | None = None) -> ToolResult:
        directory = args.get("directory", ".")
        recursive = args.get("recursive", False)

        try:
            path = _resolve_path(directory, context, allow_workspace_escape=_is_bypass_mode(context))
        except PathTraversalError as exc:
            return self._error_result(str(exc))

        if not path.exists():
            return self._error_result(f"Directory does not exist: {directory}")
        if not path.is_dir():
            return self._error_result(f"Not a directory: {directory}")

        cached_result, stale_cache = _lookup_list_files_cache_result(path, recursive)
        signature = args_signature({"directory": str(path), "recursive": bool(recursive)})
        if cached_result is not None:
            await emit_cache_metric(
                context,
                cache_layer="list_files.result",
                tool_name=self.name,
                args_signature_value=signature,
                hit=True,
                payload_size_bytes=len(cached_result.encode("utf-8")),
            )
            return self._success_result(cached_result)

        entries: list[str] = []
        try:
            if recursive:
                for item in sorted(path.rglob("*")):
                    # Skip hidden files and __pycache__.
                    parts = item.relative_to(path).parts
                    if any(p.startswith(".") or p == "__pycache__" for p in parts) or is_windows_reserved_path(item.relative_to(path)):
                        continue
                    rel = item.relative_to(path)
                    if item.is_dir():
                        entries.append(f"  {rel}/")
                    else:
                        size = item.stat().st_size
                        entries.append(f"  {rel}  ({_format_size(size)})")
                    if len(entries) >= LIST_FILES_MAX_ENTRIES:
                        break
            else:
                for item in sorted(path.iterdir()):
                    if item.name.startswith(".") or item.name == "__pycache__" or is_windows_reserved_path(item.name):
                        continue
                    if item.is_dir():
                        entries.append(f"  {item.name}/")
                    else:
                        size = item.stat().st_size
                        entries.append(f"  {item.name}  ({_format_size(size)})")
                    if len(entries) >= LIST_FILES_MAX_ENTRIES:
                        break
        except PermissionError:
            return self._error_result(f"No permission to access directory: {directory}")

        total = len(entries)
        header = f"{directory}/ ({total} entries)"
        if total >= LIST_FILES_MAX_ENTRIES:
            header += f" [truncated at {LIST_FILES_MAX_ENTRIES} entries]"

        result = header + "\n" + "\n".join(entries)
        _put_list_files_cache(path, recursive, result)
        await emit_cache_metric(
            context,
            cache_layer="list_files.result",
            tool_name=self.name,
            args_signature_value=signature,
            hit=False,
            stale=stale_cache,
            payload_size_bytes=len(result.encode("utf-8")),
        )
        return self._success_result(result)
