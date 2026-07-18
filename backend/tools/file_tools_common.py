"""Shared helpers for file tools, extracted from file_tools.py.

Constants + validation/diff/atomic-write/cache helpers used across
ReadFile/WriteFile/EditFile/ListFiles. Path resolution lives in path_resolution.
"""
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

# Inline-read budget in estimated tokens (len//4). cc's Read default is 25000
# tokens; 2000 was far too low and forced normal source files into artifacts,
# leaving the model with only a preview. Raised to 8000 tokens (~32K chars) to
# keep ordinary files inline while still artifact-izing very large ones.
READ_FILE_TOKEN_LIMIT = 8000  # ~32000 characters (estimated at len//4).
# Preview kept consistent with the inline budget so artifact-ized files still
# surface a substantial head (~32K chars ≈ the inline token budget).
READ_FILE_CONTEXT_PREVIEW_CHARS = 32_000
LIST_FILES_MAX_ENTRIES = 100
LIST_FILES_CACHE_TTL_SECONDS = 8.0
LIST_FILES_CACHE_MAX_ENTRIES = 128
WRITE_DIFF_EVENT_MAX_CHARS = 80_000


@dataclass
class _ListFilesCacheEntry:
    expires_at: float
    directory_mtime_ns: int
    result: str


_list_files_cache: OrderedDict[str, _ListFilesCacheEntry] = OrderedDict()
_list_files_cache_lock = Lock()


def _path_arg(args: dict[str, Any]) -> str:
    """Accept common path aliases while keeping file_path canonical."""
    value = args.get("file_path") or args.get("path") or args.get("target") or args.get("filename") or ""
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
    name, value = _first_present_arg(args, "file_path", "path", "target", "filename")
    if value is None:
        return ""
    if not isinstance(value, str):
        return f"{name} must be a workspace file path string; received {type(value).__name__}."
    return ""


def _list_files_cache_key(path: Path, recursive: bool) -> str:
    return f"{path.resolve().as_posix()}::recursive={int(bool(recursive))}"


def _get_cached_list_files_result(path: Path, recursive: bool) -> str | None:
    result, _stale = _lookup_list_files_cache_result(path, recursive)
    return result


def _lookup_list_files_cache_result(path: Path, recursive: bool) -> tuple[str | None, bool]:
    cache_key = _list_files_cache_key(path, recursive)
    now = time.monotonic()

    with _list_files_cache_lock:
        entry = _list_files_cache.get(cache_key)
        if entry is None:
            return None, False
        if now > entry.expires_at:
            _list_files_cache.pop(cache_key, None)
            return None, True

    try:
        current_mtime_ns = path.stat().st_mtime_ns
    except OSError:
        with _list_files_cache_lock:
            _list_files_cache.pop(cache_key, None)
        return None, True

    with _list_files_cache_lock:
        entry = _list_files_cache.get(cache_key)
        if entry is None:
            return None, False
        if current_mtime_ns != entry.directory_mtime_ns:
            _list_files_cache.pop(cache_key, None)
            return None, True
        _list_files_cache.move_to_end(cache_key)
        return entry.result, False


def _put_list_files_cache(path: Path, recursive: bool, result: str) -> None:
    try:
        directory_mtime_ns = path.stat().st_mtime_ns
    except OSError:
        return

    cache_key = _list_files_cache_key(path, recursive)
    entry = _ListFilesCacheEntry(
        expires_at=time.monotonic() + LIST_FILES_CACHE_TTL_SECONDS,
        directory_mtime_ns=directory_mtime_ns,
        result=result,
    )

    with _list_files_cache_lock:
        _list_files_cache[cache_key] = entry
        _list_files_cache.move_to_end(cache_key)
        while len(_list_files_cache) > LIST_FILES_CACHE_MAX_ENTRIES:
            _list_files_cache.popitem(last=False)


def clear_list_files_cache() -> None:
    """Clear in-memory list_files cache."""
    with _list_files_cache_lock:
        _list_files_cache.clear()
    # Also invalidate grep/glob result caches: an edit/write/patch changes file
    # contents, so a cached grep would otherwise return stale results for up to
    # the search-cache TTL (edit → grep verification bug). Lazy import avoids a
    # module-load cycle.
    try:
        from backend.tools.search_tools import clear_search_caches

        clear_search_caches()
    except Exception:
        pass


def _add_line_numbers(content: str, start_line: int = 1) -> str:
    """Add cat -n style line numbers (Claude Code pattern).

    Format: right-aligned 6-digit line number + "→" + content.
    Skip for content over 2000 lines to keep output manageable.
    """
    lines = content.split("\n")
    if len(lines) > 2000:
        return content
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


def _partial_text(value: str, ratio: float) -> str:
    if ratio >= 1:
        return value
    if not value:
        return ""
    lines = value.splitlines(keepends=True)
    if len(lines) > 1:
        count = max(1, min(len(lines), int(len(lines) * ratio)))
        return "".join(lines[:count])
    count = max(1, min(len(value), int(len(value) * ratio)))
    return value[:count]


async def _emit_write_preview_progress(
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
    ratios = (0.24, 0.58, 1.0)
    for index, ratio in enumerate(ratios, start=1):
        preview_content = _partial_text(new_content, ratio)
        patch, additions, deletions, truncated = _generate_limited_unified_diff(
            old_content,
            preview_content,
            display_path or file_path,
            max_chars=WRITE_DIFF_EVENT_MAX_CHARS,
        )
        if not patch.strip() and additions == 0 and deletions == 0:
            continue
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
                "progress": ratio,
            })
        except Exception:
            return
        if index < len(ratios):
            await asyncio.sleep(0.06)


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _atomic_write_text(path: Path, content: str) -> None:
    """Write UTF-8 text via a same-directory temp file, then atomically replace."""
    tmp_name = ""
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        tmp_name = ""
    finally:
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass


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
        return False, f"Unable to read current file for guarded write: {exc}"

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
    "READ_FILE_TOKEN_LIMIT", "READ_FILE_CONTEXT_PREVIEW_CHARS",
    "LIST_FILES_MAX_ENTRIES", "LIST_FILES_CACHE_TTL_SECONDS",
    "LIST_FILES_CACHE_MAX_ENTRIES", "WRITE_DIFF_EVENT_MAX_CHARS",
    "MAX_FILE_READ_BYTES",
    "_ListFilesCacheEntry", "_path_arg", "_first_present_arg",
    "_validate_text_arg", "_validate_path_arg_type",
    "_list_files_cache_key", "_get_cached_list_files_result",
    "_lookup_list_files_cache_result", "_put_list_files_cache", "clear_list_files_cache",
    "_add_line_numbers", "_generate_unified_diff",
    "_generate_limited_unified_diff", "_count_unified_diff_changes",
    "_workspace_display_path", "_partial_text", "_emit_write_preview_progress",
    "content_hash", "_atomic_write_text", "_validate_expected_hash",
    "_coerce_line_number", "_read_text_range", "_format_size",
]
