"""Shared helpers for file tools, extracted from file_tools.py.

Constants + validation/diff/atomic-write/cache helpers used across
ReadFile/WriteFile/EditFile/ListFiles. Path resolution lives in path_resolution.
"""
from __future__ import annotations

import difflib
import os
import hashlib
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any

from backend.artifact.store import ArtifactStore
from backend.permissions.context import ToolExecutionContext
from backend.security.sensitive_files import is_protected_write_path, is_sensitive_file
from backend.atomic_io import atomic_write_text
from backend.tools.base import (
    MAX_TOOL_RESULT_BYTES,
    MAX_TOOL_RESULT_LINES,
    BaseTool,
    PermissionLevel,
    ToolResult,
    ToolSchema,
)
from backend.tools.path_resolution import PathTraversalError, _is_bypass_mode, _resolve_path
from backend.workspace.path_filters import is_windows_reserved_path

# Pi's Read contract: complete lines up to 2000 lines or 50 KiB, then continue
# with offset/limit. Keep the old token names as derived compatibility aliases.
READ_FILE_MAX_LINES = MAX_TOOL_RESULT_LINES
READ_FILE_MAX_BYTES = MAX_TOOL_RESULT_BYTES
READ_FILE_TOKEN_LIMIT = READ_FILE_MAX_BYTES // 4
READ_FILE_CONTEXT_PREVIEW_CHARS = READ_FILE_MAX_BYTES
# Pi's ls contract: the caller may choose the entry limit and the default is
# 500. This is an output contract, not a hidden traversal cap.
LIST_FILES_MAX_ENTRIES = 500
# Diff previews use the same output budget as other tool results (Pi's
# 50-KiB/2000-line contract) instead of a second local threshold.
WRITE_DIFF_EVENT_MAX_CHARS = MAX_TOOL_RESULT_BYTES

def _path_arg(args: dict[str, Any]) -> str:
    value = args.get("file_path") or ""
    return str(value).strip()


def _first_present_arg(args: dict[str, Any], *names: str) -> tuple[str, Any]:
    for name in names:
        if name in args:
            return name, args.get(name)
    return names[0] if names else "", None


def _validate_text_arg(args: dict[str, Any], *names: str, role: str) -> str:
    name, value = _first_present_arg(args, *names)
    if value is None:
        return ""
    if not isinstance(value, str):
        return f"{name} must be a string containing {role}; received {type(value).__name__}."
    return ""


def _validate_path_arg_type(args: dict[str, Any]) -> str:
    name, value = _first_present_arg(args, "file_path")
    if value is None:
        return ""
    if not isinstance(value, str):
        return f"{name} must be a workspace file path string; received {type(value).__name__}."
    return ""


@dataclass(frozen=True)
class _ListFilesCacheEntry:
    dependencies: tuple[Path, ...]
    signature: tuple[tuple[str, int, int, int], ...]
    result: str


_LIST_FILES_CACHE: dict[tuple[str, bool, int | None], _ListFilesCacheEntry] = {}
_LIST_FILES_CACHE_LOCK = Lock()


def _list_files_signature(paths: tuple[Path, ...]) -> tuple[tuple[str, int, int, int], ...] | None:
    signature: list[tuple[str, int, int, int]] = []
    try:
        for dependency in paths:
            stat = dependency.stat()
            signature.append((str(dependency), stat.st_mtime_ns, stat.st_size, stat.st_mode))
    except OSError:
        return None
    return tuple(signature)


def _get_cached_list_files_result(
    path: Path,
    recursive: bool,
    *,
    limit: int | None = None,
) -> str | None:
    result, found = _lookup_list_files_cache_result(path, recursive, limit=limit)
    return result if found else None


def _lookup_list_files_cache_result(
    path: Path,
    recursive: bool,
    *,
    limit: int | None = None,
) -> tuple[str | None, bool]:
    key = (str(path.resolve()), bool(recursive), limit)
    with _LIST_FILES_CACHE_LOCK:
        entry = _LIST_FILES_CACHE.get(key)
    if entry is None:
        return None, False
    current_signature = _list_files_signature(entry.dependencies)
    if current_signature is None or current_signature != entry.signature:
        with _LIST_FILES_CACHE_LOCK:
            _LIST_FILES_CACHE.pop(key, None)
        return None, False
    return entry.result, True


def _put_list_files_cache(
    path: Path,
    recursive: bool,
    result: str,
    *,
    limit: int | None = None,
    dependencies: tuple[Path, ...] | None = None,
) -> None:
    resolved = path.resolve()
    dependency_paths = dependencies or (resolved,)
    normalized_dependencies = tuple(
        sorted({dependency.resolve() for dependency in dependency_paths}, key=lambda item: str(item).casefold())
    )
    signature = _list_files_signature(normalized_dependencies)
    if signature is None:
        return
    key = (str(resolved), bool(recursive), limit)
    with _LIST_FILES_CACHE_LOCK:
        _LIST_FILES_CACHE[key] = _ListFilesCacheEntry(
            dependencies=normalized_dependencies,
            signature=signature,
            result=result,
        )


def clear_list_files_cache() -> None:
    """Invalidate cached directory listings after workspace mutations."""
    with _LIST_FILES_CACHE_LOCK:
        _LIST_FILES_CACHE.clear()


def _add_line_numbers(content: str, start_line: int = 1) -> str:
    """Add cat -n style line numbers (Claude Code pattern).

    Format: right-aligned 6-digit line number + "→" + content. Claude Code's
    addLineNumbers (utils/file.ts) never opts out — it bounds the *read*
    (MAX_LINES_TO_READ) instead of silently dropping the prefix. We bound the
    read via the Pi line/byte contract, so always number here: previously a file
    over 2000 lines was returned without line numbers, yet edit_file tells the
    model to strip the line-number prefix — so the model had nothing to strip
    and risked mangling real content.
    """
    lines = content.split("\n")
    width = max(6, len(str(len(lines) + start_line - 1)))
    result = []
    for i, line in enumerate(lines):
        num = start_line + i
        result.append(f"{num:>{width}}→{line}")
    return "\n".join(result)


def _generate_unified_diff(old_content: str, new_content: str, file_path: str | Path) -> str:
    """Generate a unified diff (git diff style) between old and new content."""
    patch, _additions, _deletions, _truncated = _generate_limited_unified_diff(
        old_content,
        new_content,
        file_path,
        max_chars=None,
    )
    return patch


def _generate_limited_unified_diff(
    old_content: str,
    new_content: str,
    file_path: str | Path,
    *,
    max_chars: int | None,
) -> tuple[str, int, int, bool]:
    """Generate a unified diff preview while counting the full change size."""
    old_lines = old_content.splitlines(keepends=True)
    new_lines = new_content.splitlines(keepends=True)

    # difflib.unified_diff requires each line to end with '\n'.
    old_lines = [line if line.endswith('\n') else line + '\n' for line in old_lines]
    new_lines = [line if line.endswith('\n') else line + '\n' for line in new_lines]

    path_str = str(file_path)
    diff = difflib.unified_diff(
        old_lines,
        new_lines,
        fromfile=f"a/{path_str}",
        tofile=f"b/{path_str}",
    )

    additions = 0
    deletions = 0
    kept: list[str] = []
    kept_chars = 0
    truncated = False
    for line in diff:
        if not line.startswith(("+++", "---")):
            if line.startswith("+"):
                additions += 1
            elif line.startswith("-"):
                deletions += 1
        if max_chars is None or kept_chars + len(line) <= max_chars:
            kept.append(line)
            kept_chars += len(line)
        else:
            truncated = True

    if truncated:
        kept.append(
            "\n... [diff truncated; file was written successfully and "
            "the full content is available on disk] ...\n"
        )
    return ''.join(kept), additions, deletions, truncated


def _count_unified_diff_changes(patch: str) -> tuple[int, int]:
    additions = 0
    deletions = 0
    for line in patch.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            additions += 1
        elif line.startswith("-"):
            deletions += 1
    return additions, deletions


def _workspace_display_path(path: Path, raw_path: str, context: ToolExecutionContext | None) -> str:
    workspace_root = Path(context.workspace_root).resolve() if context and context.workspace_root else None
    if workspace_root:
        try:
            return path.resolve().relative_to(workspace_root).as_posix()
        except ValueError:
            return path.resolve().as_posix()
    return raw_path


async def _emit_write_diff(
    context: ToolExecutionContext | None,
    *,
    file_path: str,
    old_content: str,
    new_content: str,
    display_path: str,
) -> None:
    emit = getattr(context, "emit_event", None) if context else None
    if emit is None:
        return
    tool_call_id = str((context.metadata or {}).get("_current_tool_call_id") or "file-write")
    patch, additions, deletions, truncated = _generate_limited_unified_diff(
        old_content,
        new_content,
        display_path or file_path,
        max_chars=WRITE_DIFF_EVENT_MAX_CHARS,
    )
    if not patch.strip() and additions == 0 and deletions == 0:
        return
    try:
        await emit("diff.git_working_tree", {
            "files": [{
                "path": display_path or file_path,
                "patch": patch,
                "additions": additions,
                "deletions": deletions,
                "is_binary": False,
                "is_truncated": truncated,
            }],
            "untracked": [],
            "preview": True,
            "tool_call_id": tool_call_id,
        })
    except Exception:
        return


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _atomic_write_text(path: Path, content: str) -> None:
    """Compatibility wrapper for the shared durable writer."""
    atomic_write_text(path, content)


def _validate_expected_hash(path: Path, expected_hash: Any) -> tuple[bool, str]:
    if not path.exists():
        if str(expected_hash or "").strip():
            return False, "expected_hash must be empty when creating a new file"
        return True, ""

    normalized = str(expected_hash or "").strip().lower()
    if not normalized:
        return (
            False,
            "expected_hash is required for existing files. Re-read the file with read_file and retry with its content_hash.",
        )
    try:
        current_content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return False, "Only UTF-8 text files support guarded writes"
    except OSError as exc:
        return False, f"Unable to read current file for guarded write ({type(exc).__name__}, errno={exc.errno})"

    actual_hash = content_hash(current_content)
    if actual_hash != normalized:
        return (
            False,
            f"File changed on disk; expected_hash={normalized}, actual_hash={actual_hash}. Re-read before editing.",
        )
    return True, ""


MAX_FILE_READ_BYTES = 10 * 1024 * 1024  # 10 MB


def _coerce_line_number(value: Any, *, default: int | None = None) -> int | None:
    if value is None:
        return default
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _read_text_range(
    path: Path,
    *,
    start_line: int,
    end_line: int | None,
    max_bytes: int,
) -> str:
    selected: list[str] = []
    selected_bytes = 0

    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if line_number < start_line:
                continue
            if end_line is not None and line_number > end_line:
                break
            selected.append(line)
            selected_bytes += len(line.encode("utf-8"))
            if selected_bytes > max_bytes:
                raise ValueError(
                    f"Requested line range is too large; narrow start_line/end_line to stay under {max_bytes // 1024 // 1024}MB"
                )

    return "".join(selected)

def _format_size(size: int) -> str:
    """Format a byte count for display."""
    if size < 1024:
        return f"{size} B"
    elif size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    else:
        return f"{size / (1024 * 1024):.1f} MB"

__all__ = [
    "READ_FILE_MAX_LINES", "READ_FILE_MAX_BYTES", "READ_FILE_TOKEN_LIMIT", "READ_FILE_CONTEXT_PREVIEW_CHARS",
    "LIST_FILES_MAX_ENTRIES", "WRITE_DIFF_EVENT_MAX_CHARS",
    "MAX_FILE_READ_BYTES",
    "_path_arg", "_first_present_arg",
    "_validate_text_arg", "_validate_path_arg_type",
    "_get_cached_list_files_result",
    "_lookup_list_files_cache_result", "_put_list_files_cache", "clear_list_files_cache",
    "_add_line_numbers", "_generate_unified_diff",
    "_generate_limited_unified_diff", "_count_unified_diff_changes",
    "_workspace_display_path", "_emit_write_diff",
    "content_hash", "_atomic_write_text", "_validate_expected_hash",
    "_coerce_line_number", "_read_text_range", "_format_size",
]
