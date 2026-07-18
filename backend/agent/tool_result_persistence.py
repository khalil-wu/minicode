"""Disk persistence for large tool results.

Mirrors Claude Code's ``toolResultStorage.ts`` pattern: when a tool result
exceeds the inline threshold, the full content is written to a file on disk
and the in-context message is replaced with a compact preview that includes
a reference the model can use to re-read the full output via ``read_file``.

Unlike the head/tail truncation in ``_micro_compact``, this preserves the
*complete* original content — it is not lost, just moved to secondary storage.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from backend.config import DATA_ROOT

logger = logging.getLogger(__name__)

# Directory for persisted tool results
TOOL_RESULT_DATA_DIR = DATA_ROOT / "tool-results"

# Persist results larger than this threshold (in characters).
# Below this, inline content is kept as-is.
PERSIST_THRESHOLD_CHARS = 20_000

# Maximum preview shown inline for persisted results.
PERSISTED_PREVIEW_HEAD = 1_200
PERSISTED_PREVIEW_TAIL = 600

# Tools whose results are eligible for disk persistence.
# Only idempotent / re-fetchable tools are included — stateful tool output
# (e.g. task results, artifact reads) is excluded because it cannot be
# recovered and would be misleadingly truncated.
PERSISTABLE_TOOLS = frozenset({
    "read_file", "list_files", "grep_files", "glob_files", "fuzzy_search",
    "git_status", "git_diff", "git_log", "web_fetch", "web_search",
    "run_command", "go_to_definition", "find_references",
})

_LOCK = threading.Lock()
_INITIALIZED = False


def _ensure_dir() -> None:
    global _INITIALIZED
    if _INITIALIZED:
        return
    with _LOCK:
        if not _INITIALIZED:
            TOOL_RESULT_DATA_DIR.mkdir(parents=True, exist_ok=True)
            _INITIALIZED = True


@dataclass(frozen=True)
class PersistedToolResult:
    """Metadata for a tool result persisted to disk."""

    filepath: str
    original_chars: int
    original_lines: int
    content_hash: str
    preview: str
    tool_call_id: str
    tool_name: str

    @property
    def saved_chars(self) -> int:
        return max(0, self.original_chars - len(self.preview))


def _build_preview(content: str, tool_name: str, tool_call_id: str) -> str:
    """Build a compact inline preview for a persisted tool result."""
    head = content[:PERSISTED_PREVIEW_HEAD]
    tail = content[-PERSISTED_PREVIEW_TAIL:] if len(content) > PERSISTED_PREVIEW_HEAD + PERSISTED_PREVIEW_TAIL else ""
    omitted = max(0, len(content) - PERSISTED_PREVIEW_HEAD - PERSISTED_PREVIEW_TAIL)

    parts = [
        "<persisted-tool-result>",
        f"Tool: {tool_name} | call_id: {tool_call_id}",
        f"Original size: {len(content)} chars, {content.count(chr(10)) + 1} lines",
        f"Full output saved to disk. Use read_file with the path below to retrieve it.",
        "",
        "--- preview (head) ---",
        head,
    ]
    if omitted > 0:
        parts.append(f"... [{omitted} chars omitted — read the file for full content] ...")
        parts.append("--- preview (tail) ---")
        parts.append(tail)
    parts.append("</persisted-tool-result>")
    return "\n".join(parts)


def persist_tool_result(
    content: str,
    tool_call_id: str,
    tool_name: str,
) -> PersistedToolResult | None:
    """Persist a large tool result to disk and return metadata + preview.

    Returns ``None`` if persistence fails or the result is too small.
    The caller should replace the inline content with ``result.preview``
    when a ``PersistedToolResult`` is returned.
    """
    if not content or len(content) < PERSIST_THRESHOLD_CHARS:
        return None
    if tool_name not in PERSISTABLE_TOOLS:
        return None

    _ensure_dir()

    # Deterministic filename: hash of content so identical results are deduped.
    content_hash = hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()[:16]
    safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in (tool_call_id or "unknown"))
    filename = f"{safe_id}_{content_hash}.txt"
    filepath = TOOL_RESULT_DATA_DIR / filename

    try:
        # Use 'x' flag to create exclusively — dedupes concurrent writes.
        if not filepath.exists():
            filepath.write_text(content, encoding="utf-8")
    except OSError as exc:
        logger.warning("Failed to persist tool result to %s: %s", filepath, exc)
        return None

    preview = _build_preview(content, tool_name, tool_call_id)

    logger.info(
        "[ToolResultPersistence] Persisted %s result for %s: %d chars → %d inline (%d saved)",
        tool_name,
        tool_call_id,
        len(content),
        len(preview),
        len(content) - len(preview),
    )

    return PersistedToolResult(
        filepath=str(filepath),
        original_chars=len(content),
        original_lines=content.count("\n") + 1,
        content_hash=content_hash,
        preview=preview,
        tool_call_id=tool_call_id,
        tool_name=tool_name,
    )


def try_persist_tool_result(
    content: str,
    tool_call_id: str,
    tool_name: str,
) -> str:
    """Try to persist a tool result; return the preview or the original content.

    This is the main entry point for integration — if persistence succeeds,
    the preview (with disk reference) is returned; otherwise the original
    content is returned unchanged.
    """
    if not content or len(content) < PERSIST_THRESHOLD_CHARS:
        return content
    if tool_name not in PERSISTABLE_TOOLS:
        return content
    persisted = persist_tool_result(content, tool_call_id, tool_name)
    if persisted is not None:
        return persisted.preview
    return content


def force_persist_for_compaction(content: str, tool_name: str) -> str | None:
    """Last-resort disk store before ``_micro_compact`` drops the middle.

    Unlike :func:`persist_tool_result`, this does NOT gate on the
    PERSISTABLE_TOOLS whitelist or the size threshold — it exists so that
    head/tail truncation never destroys content irrecoverably.  The filename
    is derived from the content hash alone (deduped), so no tool_call_id is
    required.  Returns the file path on success, or ``None`` if the write
    failed (caller must then annotate the loss honestly).
    """
    if not content:
        return None
    _ensure_dir()
    content_hash = hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()[:16]
    safe_tool = "".join(c if c.isalnum() or c in "-_" else "_" for c in (tool_name or "tool"))
    filename = f"mc_{safe_tool}_{content_hash}.txt"
    filepath = TOOL_RESULT_DATA_DIR / filename
    try:
        if not filepath.exists():
            filepath.write_text(content, encoding="utf-8")
    except OSError as exc:
        logger.warning("Failed to force-persist tool result to %s: %s", filepath, exc)
        return None
    return str(filepath)


def cleanup_old_results(max_age_seconds: float = 86400 * 7) -> int:
    """Remove persisted tool result files older than ``max_age_seconds``.

    Returns the number of files removed.
    """
    if not TOOL_RESULT_DATA_DIR.exists():
        return 0
    import time
    now = time.time()
    removed = 0
    try:
        for entry in TOOL_RESULT_DATA_DIR.iterdir():
            if not entry.is_file():
                continue
            try:
                mtime = entry.stat().st_mtime
            except OSError:
                continue
            if now - mtime > max_age_seconds:
                try:
                    entry.unlink()
                    removed += 1
                except OSError:
                    pass
    except OSError as exc:
        logger.debug("Tool result cleanup failed: %s", exc)
    return removed
