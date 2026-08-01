"""Disk persistence for large tool results.

Mirrors Claude Code's ``toolResultStorage.ts`` pattern: when a tool result
exceeds the inline threshold, the full content is written to a file on disk
and the in-context message is replaced with a compact preview that includes
a reference the model can use to re-read the full output via ``read_file``.

The complete original content is preserved; it is moved to secondary storage.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from dataclasses import dataclass
from pathlib import Path

from backend.config import DATA_ROOT
from backend.atomic_io import atomic_write_text
from backend.tools.base import MAX_TOOL_RESULT_BYTES

logger = logging.getLogger(__name__)

# Directory for persisted tool results
TOOL_RESULT_DATA_DIR = DATA_ROOT / "tool-results"

# Claude Code's tool-result storage threshold is shared with Pi's 50-KiB
# model-facing result budget. Keep the legacy name for integrations, but
# measure UTF-8 bytes rather than Python code points.
PERSIST_THRESHOLD_BYTES = MAX_TOOL_RESULT_BYTES
PERSIST_THRESHOLD_CHARS = PERSIST_THRESHOLD_BYTES
_DEFAULT_PERSIST_THRESHOLD_BYTES = MAX_TOOL_RESULT_BYTES

# Claude Code's tool-result storage keeps the first 2 KB inline. This is a
# preview contract, not a second execution/output limit.
PERSISTED_PREVIEW_CHARS = 2_000

_LOCK = threading.Lock()
_INITIALIZED = False


def _effective_persist_threshold_bytes() -> int:
    """Honor the legacy override without replacing the UTF-8 byte contract."""
    if (
        PERSIST_THRESHOLD_BYTES == _DEFAULT_PERSIST_THRESHOLD_BYTES
        and PERSIST_THRESHOLD_CHARS != _DEFAULT_PERSIST_THRESHOLD_BYTES
    ):
        return max(0, int(PERSIST_THRESHOLD_CHARS))
    return max(0, int(PERSIST_THRESHOLD_BYTES))


def is_tool_result_path(path: str | Path) -> bool:
    """Return whether *path* is an existing file in MiniCode's read-only cache.

    Resolution is performed before the containment check so junctions and
    symlinks cannot turn the cache exception into an arbitrary filesystem read.
    This helper is shared by the agent permission layer and ``read_file`` path
    resolver; keeping the rule in one place prevents the UI and model from
    disagreeing about whether a persisted result is readable.
    """
    try:
        resolved = Path(path).expanduser().resolve()
        root = TOOL_RESULT_DATA_DIR.expanduser().resolve()
        resolved.relative_to(root)
        return resolved.is_file()
    except (OSError, RuntimeError, ValueError):
        return False


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


def _build_preview(
    content: str,
    tool_name: str,
    tool_call_id: str,
    filepath: Path,
) -> str:
    """Build a compact inline preview for a persisted tool result."""
    preview = content[:PERSISTED_PREVIEW_CHARS]
    parts = [
        "<persisted-output>",
        f"Output too large ({len(content)} chars). Full output saved to: {filepath}",
        "",
        f"Preview (first {PERSISTED_PREVIEW_CHARS} chars):",
        preview,
    ]
    if len(content) > len(preview):
        parts.append("...")
    parts.append("</persisted-output>")
    return "\n".join(parts)


def persist_tool_result(
    content: str,
    tool_call_id: str,
    tool_name: str,
    *,
    force: bool = False,
) -> PersistedToolResult | None:
    """Persist a large tool result to disk and return metadata + preview.

    Returns ``None`` if persistence fails or the result is too small.
    The caller should replace the inline content with ``result.preview``
    when a ``PersistedToolResult`` is returned.
    """
    content_bytes = len(content.encode("utf-8", errors="replace")) if content else 0
    if not content or (not force and content_bytes <= _effective_persist_threshold_bytes()):
        return None
    _ensure_dir()

    # Deterministic filename: hash of content so identical results are deduped.
    content_hash = hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()
    safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in (tool_call_id or "unknown"))
    filename = f"{safe_id}_{content_hash}.txt"
    filepath = TOOL_RESULT_DATA_DIR / filename

    try:
        # Publish through the shared atomic writer. Identical content hashes
        # make concurrent writers harmless and avoid exposing partial files.
        with _LOCK:
            if not filepath.exists():
                atomic_write_text(filepath, content)
    except OSError as exc:
        logger.warning("Failed to persist tool result to %s: %s", filepath, exc)
        return None

    preview = _build_preview(content, tool_name, tool_call_id, filepath)

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
    if not content or len(content.encode("utf-8", errors="replace")) <= _effective_persist_threshold_bytes():
        return content
    persisted = persist_tool_result(content, tool_call_id, tool_name)
    if persisted is not None:
        return persisted.preview
    return content


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
