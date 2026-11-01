"""Pi-compatible directory listing tool."""
from __future__ import annotations

from typing import Any
from contextlib import ExitStack
from pathlib import Path
import os
import stat

from backend.permissions.context import ToolExecutionContext
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema
from backend.tools.file_tools_common import LIST_FILES_MAX_ENTRIES
from backend.tools.path_resolution import (
    PathTraversalError,
    _is_bypass_mode,
    _resolve_path,
)
from backend.workspace.path_filters import is_windows_reserved_path

class ListFilesTool(BaseTool):
    requires_execution_environment = True
    """
    List directory contents.

    Returns at most ``limit`` entries (defaulting to Pi's 500-entry contract).
    """

    name = "list_files"
    read_only = True
    result_kind = "file"
    activity_kind = "workspaceSearch"
    display_label = "List"
    description = (
        "List files and directories with sizes/types. Use to explore project structure; use glob_files for name patterns and grep_files for content."
    )
    permission = PermissionLevel.AUTO
    workspace_path_fields = ("path", "directory")
    allow_workspace_root_path = True

    def model_description(self) -> str:
        return "List files and directories to inspect project structure."

    def model_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.model_description(),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory to list (default: workspace root)."},
                    "directory": {"type": "string", "description": "Alternative directory path; use path when both are specified."},
                    "recursive": {"type": "boolean", "description": "List descendants up to the entry limit; does not follow directory symlinks.", "default": False},
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "Maximum number of entries to return (default 500).",
                    },
                },
                "required": [],
            },
        )

    def get_spec(self):
        from backend.tools.contracts import ToolSpec

        return ToolSpec(
            name=self.name,
            capability="workspace.list",
            exposure="core",
        )

    def streamed_input_preview(
        self,
        args: dict[str, Any],
        context: Any | None = None,
        prior: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        preview: dict[str, Any] = {}
        if isinstance(args.get("path"), str):
            preview["path"] = args["path"]
        limit = args.get("limit")
        if isinstance(limit, int) and not isinstance(limit, bool):
            preview["limit"] = limit
        return preview

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Directory to list; defaults to the workspace root.",
                    },
                    "directory": {"type": "string", "description": "Alternative directory path; use path when both are specified."},
                    "recursive": {"type": "boolean", "description": "List descendants up to the entry limit; does not follow directory symlinks.", "default": False},
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "Maximum number of entries to return. Default 500.",
                    },
                },
                "required": [],
            },
        )

    async def execute(self, args: dict[str, Any], context: ToolExecutionContext | None = None) -> ToolResult:
        from backend.workspace.executor_service import uses_executor_io
        if uses_executor_io(context):
            from backend.workspace.executor_tool_io import executor_list_files
            return await executor_list_files(self, args, context)
        directory = args.get("path") or args.get("directory", ".")
        recursive = args.get("recursive", False)
        raw_limit = args.get("limit")
        if raw_limit is None:
            limit = LIST_FILES_MAX_ENTRIES
        else:
            try:
                limit = int(raw_limit)
            except (TypeError, ValueError):
                return self._error_result("limit must be a positive integer")
            if limit < 1:
                return self._error_result("limit must be a positive integer")

        try:
            path = _resolve_path(
                directory,
                context,
                allow_workspace_escape=_is_bypass_mode(context),
                allow_declared_read_root=True,
            )
        except PathTraversalError as exc:
            return self._error_result(str(exc))

        if not path.exists():
            return self._error_result(f"Directory does not exist: {directory}")
        if not path.is_dir():
            return self._error_result(f"Not a directory: {directory}")

        # A listing is a read of the workspace surface, so it must hide whatever
        # read_file/grep_files/glob_files hide. Names alone still disclose
        # secrets (.env.production, secrets/api.txt, deploy.pem).
        checker = getattr(context, "permission_checker", None) if context is not None else None
        permission = getattr(context, "permission", None) if context is not None else None
        is_allowed = (
            (lambda candidate: checker.is_path_allowed(str(candidate), context=permission))
            if checker is not None
            else None
        )
        entries: list[str] = []
        entry_limit_reached = False
        try:
            # Native iterators stop at the requested prefix and close even when
            # the limit is reached. Never materialize a whole directory to sort.
            with ExitStack() as scans:
                directories = [scans.enter_context(os.scandir(path))]
                while directories:
                    entry = next(directories[-1], None)
                    if entry is None:
                        directories.pop().close()
                        continue
                    item = Path(entry.path)
                    rel = item.relative_to(path)
                    if os.name == "nt" and is_windows_reserved_path(rel):
                        continue
                    if is_allowed is not None and not is_allowed(item):
                        continue
                    is_directory = entry.is_dir(follow_symlinks=False)
                    entries.append(f"{rel.as_posix()}{'/' if is_directory else ''}")
                    if len(entries) == limit:
                        entry_limit_reached = True
                        break
                    if recursive and is_directory and not (os.name == "nt" and entry.stat(follow_symlinks=False).st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                        directories.append(scans.enter_context(os.scandir(item)))
        except OSError as exc:
            return self._error_result(f"Cannot list directory {directory}: {exc}")
        entries.sort(key=str.lower)

        if not entries:
            return self._success_result("(empty directory)")

        result = "\n".join(entries)
        if entry_limit_reached:
            result += "\n\n[The requested entry limit was reached.]"
        return self._success_result(result)
