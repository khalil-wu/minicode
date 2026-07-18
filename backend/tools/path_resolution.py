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


def _is_bypass_mode(context: Any = None) -> bool:
    permission = getattr(context, "permission", None)
    return getattr(permission, "mode", None) == "bypass"


def _resolve_path(path_str: str, context: Any = None, *, allow_workspace_escape: bool = False) -> Path:
    """
    Resolve path relative to workspace root if available.
    Validates that the resolved path stays within the workspace boundary.

    Raises:
        PathTraversalError: if the resolved path escapes workspace root.
    """
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
            raise PathTraversalError(
                f"Path escapes workspace boundary: {path_str} ({workspace_root})"
            )
    elif not workspace_root and not allow_workspace_escape:
        # No workspace_root: restrict to CWD as a safety fallback
        cwd = Path.cwd().resolve()
        try:
            resolved.relative_to(cwd)
        except ValueError:
            raise PathTraversalError(
                f"Path escapes current working directory boundary: {path_str}"
            )

    return resolved
