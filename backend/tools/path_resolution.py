"""Workspace path resolution shared by file tools (and search tools).

Extracted from file_tools.py so callers that only need path resolution (e.g.
search_tools importing PathTraversalError) don't pull in the whole file-tool
monolith. Resolved paths must stay inside the active workspace boundary.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from backend.workspace.path_filters import is_windows_reserved_path


class PathTraversalError(ValueError):
    """Raised when a resolved path escapes the workspace boundary."""


_WINDOWS_DOS_DEVICE_SUFFIX_RE = re.compile(
    r"\.(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$",
    re.IGNORECASE,
)


def windows_path_safety_reason(path_str: str) -> str | None:
    """Return a reason for Windows path spellings that need manual review.

    Detect the spelling before ``Path.resolve`` can canonicalize it into a
    safer-looking name. The non-platform-specific checks are intentional;
    NTFS workspaces can be mounted from another host OS. ADS syntax remains
    limited to Windows because that is where the kernel interprets it.
    """
    raw = str(path_str or "")

    if os.name == "nt" and ":" in raw[2:]:
        return "NTFS alternate data streams are not allowed in workspace paths"
    if re.search(r"~\d", raw):
        return "8.3 short-name path patterns are not allowed"
    if raw.startswith(("\\\\?\\", "\\\\.\\", "//?/", "//./")):
        return "Windows long/device path prefixes are not allowed"
    if raw.startswith(("\\\\", "//")):
        return "UNC/network-share paths are not allowed"

    components = [component for component in re.split(r"[\\/]+", raw) if component]
    if any(
        component not in {".", ".."}
        and component.endswith((".", " ", "\t"))
        for component in components
    ):
        return "Windows path components with trailing dots or spaces are not allowed"
    if any(_WINDOWS_DOS_DEVICE_SUFFIX_RE.search(component) for component in components):
        return "Windows DOS device-name suffixes are not allowed"
    if any(re.fullmatch(r"\.{3,}", component) for component in components):
        return "ambiguous consecutive-dot path components are not allowed"
    return None


def _is_windows_device_path(path_str: str) -> bool:
    """Reject device and UNC namespaces before ``Path.resolve`` can touch them."""
    normalized = str(path_str or "").strip().replace("/", "\\")
    # UNC resolution can initiate SMB/NTLM negotiation. File tools are
    # workspace-scoped, so network shares are not a valid path boundary.
    return normalized.startswith(("\\\\", "\\?\\", "\\??\\"))


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


def denied_path_patterns(context: Any = None) -> list[str]:
    """Return the effective path denylist for the current execution context.

    Every workspace-surface tool must refuse whatever read_file refuses:
    enforcing only the built-in sensitive-file sets let a configured denylist
    entry (settings.json, secrets/) leak through search or a directory listing.
    """
    checker = getattr(context, "permission_checker", None) if context is not None else None
    if checker is None:
        return []
    permission = getattr(context, "permission", None)
    constraints = getattr(permission, "filesystem_constraints", None) or {}
    if "denylist" in constraints:
        patterns = list(constraints["denylist"])
    else:
        settings = getattr(checker, "_settings", None)
        patterns = list(getattr(settings, "path_denylist", ()) or ())
    return [
        str(pattern).replace("\\", "/").strip()
        for pattern in patterns
        if str(pattern or "").strip()
    ]


def _is_declared_plan_path(path: Path, context: Any = None) -> bool:
    from backend.agent.plans import is_current_plan_file

    return is_current_plan_file(path, context)


def _resolve_path(
    path_str: str,
    context: Any = None,
    *,
    allow_workspace_escape: bool = False,
    allow_tool_result_root: bool = False,
    allow_declared_read_root: bool = False,
    allow_current_plan_file: bool = False,
) -> Path:
    """
    Resolve path relative to workspace root if available.
    Validates that the resolved path stays within the workspace boundary.

    Raises:
        PathTraversalError: if the resolved path escapes workspace root.
    """
    if "\x00" in str(path_str or ""):
        raise PathTraversalError("Null bytes are not allowed in paths")
    safety_reason = windows_path_safety_reason(path_str)
    if safety_reason:
        raise PathTraversalError(safety_reason)
    if _is_windows_device_path(path_str) or is_windows_reserved_path(path_str):
        raise PathTraversalError(f"Windows device paths are not allowed: {path_str}")

    workspace_bound_context = context is not None and hasattr(context, "workspace_root")
    workspace_root: Path | None = None
    if workspace_bound_context and context.workspace_root:
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
            if allow_current_plan_file and _is_declared_plan_path(resolved, context):
                return resolved
            if allow_declared_read_root and _is_declared_readable_path(resolved, context):
                return resolved
            if allow_tool_result_root:
                from backend.agent.tool_result_persistence import is_tool_result_path

                if is_tool_result_path(
                    resolved,
                    conversation_id=str(getattr(context, "conversation_id", "") or ""),
                    workspace_root=getattr(context, "workspace_root", None),
                ):
                    return resolved
            raise PathTraversalError(
                f"Path escapes workspace boundary: {path_str}"
            )
    elif not workspace_root and not allow_workspace_escape:
        # A live session explicitly carrying ``workspace_root=None`` is a
        # projectless owner.  It must not borrow the process CWD (or another
        # active project) for workspace file operations.  Standalone callers
        # without an execution context retain the historical CWD default.
        # Persisted tool results live in MiniCode's state directory, which can
        # be outside a repository/evaluation cwd (subagents commonly run with
        # an isolated cwd).  ``read_file`` explicitly opts into this read-only
        # cache via ``allow_tool_result_root``; apply that exception in the
        # no-workspace branch as well as the workspace-root branch.
        if allow_current_plan_file and _is_declared_plan_path(resolved, context):
            return resolved
        if allow_tool_result_root:
            from backend.agent.tool_result_persistence import is_tool_result_path

            if is_tool_result_path(
                resolved,
                conversation_id=str(getattr(context, "conversation_id", "") or ""),
                workspace_root=getattr(context, "workspace_root", None),
            ):
                return resolved
        if allow_declared_read_root and _is_declared_readable_path(resolved, context):
            return resolved
        if workspace_bound_context:
            raise PathTraversalError(
                "Path operations require an open workspace for this conversation"
            )
        cwd = Path.cwd().resolve()
        try:
            resolved.relative_to(cwd)
        except ValueError:
            raise PathTraversalError(
                f"Path escapes current working directory boundary: {path_str}"
            )

    return resolved
