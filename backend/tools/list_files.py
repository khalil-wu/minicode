"""Pi-compatible directory listing tool."""
from __future__ import annotations

from typing import Any

from backend.permissions.context import ToolExecutionContext
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema
from backend.tools.file_tools_common import (
    LIST_FILES_MAX_ENTRIES,
    _lookup_list_files_cache_result,
    _put_list_files_cache,
)
from backend.tools.path_resolution import (
    PathTraversalError,
    _is_bypass_mode,
    _resolve_path,
    denied_path_patterns,
)
from backend.workspace.path_filters import is_windows_reserved_path

class ListFilesTool(BaseTool):
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
        denied_patterns = denied_path_patterns(context)
        checker = getattr(context, "permission_checker", None) if context is not None else None
        permission = getattr(context, "permission", None) if context is not None else None
        is_allowed = (
            (lambda candidate: checker.is_path_allowed(str(candidate), context=permission))
            if checker is not None and denied_patterns
            else None
        )
        policy_key = "\n".join(denied_patterns) if is_allowed is not None else ""

        cached_result, cache_hit = _lookup_list_files_cache_result(
            path,
            bool(recursive),
            limit=limit,
            policy_key=policy_key,
        )
        if cache_hit:
            return self._success_result(cached_result or "")

        entries: list[str] = []
        dependencies: set[Any] = {path}
        entry_limit_reached = False
        try:
            if recursive:
                candidates = sorted(
                    path.rglob("*"),
                    key=lambda item: item.relative_to(path).as_posix().lower(),
                )
                for item in candidates:
                    try:
                        if item.is_dir():
                            dependencies.add(item)
                    except OSError:
                        continue
                for item in candidates:
                    rel = item.relative_to(path)
                    if is_windows_reserved_path(rel):
                        continue
                    if is_allowed is not None and not is_allowed(item):
                        continue
                    if len(entries) >= limit:
                        entry_limit_reached = True
                        break
                    try:
                        suffix = "/" if item.is_dir() else ""
                    except OSError:
                        continue
                    entries.append(f"{rel.as_posix()}{suffix}")
            else:
                for item in sorted(path.iterdir(), key=lambda candidate: candidate.name.lower()):
                    if is_windows_reserved_path(item.name):
                        continue
                    if is_allowed is not None and not is_allowed(item):
                        continue
                    if len(entries) >= limit:
                        entry_limit_reached = True
                        break
                    try:
                        suffix = "/" if item.is_dir() else ""
                    except OSError:
                        continue
                    entries.append(f"{item.name}{suffix}")
        except (OSError, PermissionError):
            return self._error_result(f"No permission to access directory: {directory}")

        if not entries:
            result = "(empty directory)"
            _put_list_files_cache(
                path,
                bool(recursive),
                result,
                limit=limit,
                dependencies=tuple(dependencies),
                policy_key=policy_key,
            )
            return self._success_result(result)

        result = "\n".join(entries)
        if entry_limit_reached:
            result += "\n\n[The requested entry limit was reached.]"
        _put_list_files_cache(
            path,
            bool(recursive),
            result,
            limit=limit,
            dependencies=tuple(dependencies),
            policy_key=policy_key,
        )
        return self._success_result(result)
