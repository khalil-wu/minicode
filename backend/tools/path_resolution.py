"""Workspace path resolution shared by file tools (and search tools).

Extracted from file_tools.py so callers that only need path resolution (e.g.
search_tools importing PathTraversalError) don't pull in the whole file-tool
monolith. Resolved paths must stay inside the active workspace boundary.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any


class PathTraversalError(ValueError):
    """Raised when a resolved path escapes the workspace boundary."""


def _is_windows_device_path(path_str: str) -> bool:
    """Reject Win32 device namespaces without blocking ordinary UNC shares."""
    normalized = str(path_str or "").strip().replace("/", "\\")
    return normalized.startswith(("\\\\?\\", "\\\\.\\", "\\??\\"))


def _is_bypass_mode(context: Any = None) -> bool:
    permission = getattr(context, "permission", None)
    return getattr(permission, "mode", None) == "bypass"


def _is_declared_readable_path(path: Path, context: Any = None) -> bool:
    permission = getattr(context, "permission", None)
    constraints = getattr(permission, "filesystem_constraints", {}) or {}
    for raw_root in constraints.get("readable_roots", []):
        try:
            path.relative_to(Path(str(raw_root)).expanduser().resolve())
            return True
        except (OSError, ValueError):
            continue
    return False


def _resolve_path(
    path_str: str,
    context: Any = None,
    *,
    allow_workspace_escape: bool = False,
    allow_tool_result_root: bool = False,
    allow_declared_read_root: bool = False,
) -> Path:
    """
    Resolve path relative to workspace root if available.
    Validates that the resolved path stays within the workspace boundary.

    Raises:
        PathTraversalError: if the resolved path escapes workspace root.
    """
    if _is_windows_device_path(path_str):
        raise PathTraversalError(f"Windows device paths are not allowed: {path_str}")

    workspace_root: Path | None = None
    if context and hasattr(context, 'workspace_root') and context.workspace_root:
        workspace_root = Path(context.workspace_root).resolve()

    path = Path(path_str)
    if path.is_absolute():
        resolved = path.resolve()
    elif workspace_root:
        resolved = (workspace_root / path).resolve()
    else:
        resolved = path.resolve()

    if workspace_root and not allow_workspace_escape:
        try:
            resolved.relative_to(workspace_root)
        except ValueError:
            if allow_declared_read_root and _is_declared_readable_path(resolved, context):
                return resolved
            if allow_tool_result_root:
                from backend.agent.tool_result_persistence import is_tool_result_path

                if is_tool_result_path(resolved):
                    return resolved
            raise PathTraversalError(
                f"Path escapes workspace boundary: {path_str}"
            )
    elif not workspace_root and not allow_workspace_escape:
        # No workspace_root: restrict to CWD as a safety fallback
        # Persisted tool results live in MiniCode's state directory, which can
        # be outside a repository/evaluation cwd (subagents commonly run with
        # an isolated cwd).  ``read_file`` explicitly opts into this read-only
        # cache via ``allow_tool_result_root``; apply that exception in the
        # no-workspace branch as well as the workspace-root branch.
        if allow_tool_result_root:
            from backend.agent.tool_result_persistence import is_tool_result_path

            if is_tool_result_path(resolved):
                return resolved
        if allow_declared_read_root and _is_declared_readable_path(resolved, context):
            return resolved
        cwd = Path.cwd().resolve()
        try:
            resolved.relative_to(cwd)
        except ValueError:
            raise PathTraversalError(
                f"Path escapes current working directory boundary: {path_str}"
            )

    return resolved
