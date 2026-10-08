"""Shared helpers for file tools, extracted from file_tools.py.

Constants + validation/diff/atomic-write/cache helpers used across
ReadFile/WriteFile/EditFile/ListFiles. Path resolution lives in path_resolution.
"""
from __future__ import annotations

import logging
import hashlib
from itertools import islice
from pathlib import Path
from typing import Any

from backend.agent.turn_diff_tracker import TurnDiffTracker
from backend.diff.unified import count_unified_diff_changes as _count_unified_diff_changes, iter_unified_diff
from backend.permissions.context import ToolExecutionContext
from backend.atomic_io import atomic_write_text as _atomic_write_text, canonical_path_mapping_key, normalize_text_newlines
from backend.tools.base import (
    MAX_TOOL_RESULT_BYTES,
    MAX_TOOL_RESULT_CHARS,
    MAX_TOOL_RESULT_LINES,
)

logger = logging.getLogger(__name__)

# MiniCode's Read contract: complete lines up to 2000 lines or 50 KiB, then continue
# with offset/limit. Keep the old token names as derived compatibility aliases.
READ_FILE_MAX_LINES = MAX_TOOL_RESULT_LINES
READ_FILE_MAX_BYTES = MAX_TOOL_RESULT_BYTES
# MiniCode's FileReadTool DEFAULT_MAX_OUTPUT_TOKENS = 25_000.
# The byte contract above still bounds a single read; this token ceiling is the
# separate model-facing budget cc applies, so align it rather than deriving a
# smaller value from the byte cap.
READ_FILE_TOKEN_LIMIT = 25_000
READ_FILE_CONTEXT_PREVIEW_CHARS = MAX_TOOL_RESULT_CHARS
# MiniCode's ls contract: the caller may choose the entry limit and the default is
# 500. This is an output contract, not a hidden traversal cap.
LIST_FILES_MAX_ENTRIES = 500
# Diff previews use the same output budget as other tool results (MiniCode's
# 50-KiB/2000-line contract) instead of a second local threshold.
WRITE_DIFF_EVENT_MAX_CHARS = MAX_TOOL_RESULT_CHARS

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


def invalidate_workspace_file_caches(
    *,
    file_tree_changed: bool = False,
    clear_file_state: bool = False,
) -> None:
    """Invalidate every derived workspace view affected by a file mutation.

    Dedicated file tools know the paths they changed and can invalidate one
    file-state entry directly. Shell commands do not: a command may create,
    delete, rename, or rewrite arbitrary files. Keeping this policy in one
    place keeps fuzzy discovery and the read cache consistent with mutations.

    ``clear_file_state`` is intentionally opt-in because direct file tools can
    invalidate their known paths more cheaply. It is used for shell commands,
    where the affected path set is unknowable without parsing the shell.
    """
    from backend.workspace.project_index_runtime import project_index_runtime

    project_index_runtime.invalidate_all()
    if file_tree_changed:
        from backend.workspace.fuzzy_search import invalidate_global_fuzzy_search

        invalidate_global_fuzzy_search()

    if clear_file_state:
        from backend.workspace.file_state_cache import clear_global_file_cache

        clear_global_file_cache()


def _add_line_numbers(content: str, start_line: int = 1) -> str:
    """Add cat -n style line numbers (MiniCode pattern).

    Format: right-aligned 6-digit line number + "→" + content. MiniCode's
    addLineNumbers (utils/file.ts) never opts out — it bounds the *read*
    (MAX_LINES_TO_READ) instead of silently dropping the prefix. We bound the
    read via the MiniCode line/byte contract, so always number here: previously a file
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
    path_str = str(file_path)
    diff = iter_unified_diff(
        old_content,
        new_content,
        fromfile=f"a/{path_str}",
        tofile=f"b/{path_str}",
    )

    additions = 0
    deletions = 0
    kept: list[str] = []
    kept_chars = 0
    truncated = False
    in_hunk = False
    for line in diff:
        if line.startswith("@@ "):
            in_hunk = True
        elif in_hunk:
            if line.startswith("+"):
                additions += 1
            elif line.startswith("-"):
                deletions += 1
        if not truncated and (max_chars is None or kept_chars + len(line) <= max_chars):
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
    old_content: str | None,
    new_content: str | None,
    display_path: str,
    old_display_path: str | None = None,
    overwritten_new_content: str | None = None,
) -> None:
    emit = getattr(context, "emit_event", None) if context else None
    if emit is None:
        return
    tracker = getattr(context, "turn_diff_tracker", None)
    if not isinstance(tracker, TurnDiffTracker):
        tracker = TurnDiffTracker()
        context.turn_diff_tracker = tracker
    tool_call_id = str(context.tool_call_id or "file-write")
    old_path = old_display_path or display_path or file_path
    new_path = display_path or file_path
    try:
        # Keep mutation + snapshot emission under the turn tracker lock. Tool
        # calls can run concurrently; MiniCode serializes tracker updates so an
        # older one-file snapshot can never arrive after a newer aggregate.
        async with tracker.lock:
            had_diff = tracker.has_unified_diff()
            tracker.track_change(
                old_path=old_path,
                new_path=new_path,
                old_content=old_content,
                new_content=new_content,
                overwritten_new_content=overwritten_new_content,
            )
            snapshot = tracker.snapshot()
            if not had_diff and snapshot.unified_diff is None:
                return
            from backend.agent.message import AgentEvent

            thread_id = str(getattr(context, "conversation_id", "") or "")
            turn_id = str(
                (context.metadata or {}).get("run_id")
                or (context.metadata or {}).get("turn_id")
                or (context.metadata or {}).get("assistant_message_id")
                or ""
            )
            await emit(
                "turn.diff.updated",
                AgentEvent.turn_diff_updated(
                    thread_id=thread_id,
                    turn_id=turn_id,
                    diff=snapshot.unified_diff or "",
                    revision=snapshot.revision,
                    tool_call_id=tool_call_id,
                ).data,
            )
    except Exception:
        return


def content_hash(content: str) -> str:
    # The tool read/write contract uses the same universal newlines as read_file.
    return hashlib.sha256(normalize_text_newlines(content).encode("utf-8")).hexdigest()


def record_file_hash(context: ToolExecutionContext | None, path: Path, value: str | None) -> None:
    """Advance the existing read state from content this operation observed."""
    if context is None:
        return
    hashes = context.metadata.setdefault("_read_file_hashes", {})
    key = canonical_path_mapping_key(hashes, path)
    hashes.pop(key, None)
    if value is not None:
        hashes[key] = value


def _validate_expected_hash(path: Path, expected_hash: Any, *, require_hash: bool = True) -> tuple[bool, str]:
    if not path.exists():
        if str(expected_hash or "").strip():
            return False, "expected_hash must be empty when creating a new file"
        return True, ""

    normalized = str(expected_hash or "").strip().lower()
    if not normalized:
        # A missing hash is tolerated when the caller opts out of the
        # read-before-write guard (edit_file). Correctness still relies on the
        # exact, unique old_string match enforced by prepare_edit_content, so a
        # stale mental model fails cleanly as "old_string not found" rather than
        # corrupting the file. write_file/notebook keep require_hash=True.
        if not require_hash:
            return True, ""
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
        for line in islice(handle, start_line - 1, end_line):
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
    "invalidate_workspace_file_caches",
    "_add_line_numbers", "_generate_unified_diff",
    "_generate_limited_unified_diff", "_count_unified_diff_changes",
    "_workspace_display_path", "_emit_write_diff",
    "content_hash", "_atomic_write_text", "_validate_expected_hash",
    "_read_text_range", "_format_size",
]
