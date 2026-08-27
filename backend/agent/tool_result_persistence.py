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
from backend.atomic_io import atomic_write_text, file_mutation_locks
logger = logging.getLogger(__name__)

# Directory for persisted tool results
TOOL_RESULT_DATA_DIR = DATA_ROOT / "tool-results"

# Claude Code persists ordinary text tool results above 50,000 characters
# (constants/toolLimits.ts DEFAULT_MAX_RESULT_SIZE_CHARS). This is distinct from
# Pi's 2,000-line / 50-KiB tool-specific truncation contract, which is applied by
# the tool projection layer before context is built.
PERSIST_THRESHOLD_CHARS = 50_000

# Claude Code's tool-result storage keeps the first 2 KB inline. This is a
# preview contract, not a second execution/output limit.
PERSISTED_PREVIEW_CHARS = 2_000

_LOCK = threading.Lock()
_INITIALIZED = False


def _effective_persist_threshold_chars() -> int:
    """Return Claude Code's character threshold for persisting tool results."""
    return max(0, int(PERSIST_THRESHOLD_CHARS))


def _owner_fingerprint(
    conversation_id: str = "",
    workspace_root: str | Path | None = None,
) -> str:
    conversation = str(conversation_id or "").strip()
    workspace = ""
    if workspace_root:
        try:
            workspace = str(Path(workspace_root).expanduser().resolve())
        except (OSError, RuntimeError):
            workspace = str(workspace_root)
    if not conversation and not workspace:
        return ""
    return hashlib.sha256(
        f"{conversation}\x00{workspace}".encode("utf-8", errors="replace")
    ).hexdigest()[:16]


def is_tool_result_path(
    path: str | Path,
    *,
    conversation_id: str = "",
    workspace_root: str | Path | None = None,
) -> bool:
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
        if not resolved.is_file():
            return False
        owner = _owner_fingerprint(conversation_id, workspace_root)
        if owner:
            # New persisted results carry an opaque owner fingerprint in the
            # filename.  A conversation may only dereference files created for
            # its own conversation/workspace pair; legacy unscoped files are
            # intentionally not accepted by an owner-scoped read.
            return resolved.stem.endswith(f"_{owner}")
        return True
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
    conversation_id: str = "",
    workspace_root: str | Path | None = None,
) -> PersistedToolResult | None:
    """Persist a large tool result to disk and return metadata + preview.

    Returns ``None`` if persistence fails or the result is too small.
    The caller should replace the inline content with ``result.preview``
    when a ``PersistedToolResult`` is returned.
    """
    if not content or (not force and len(content) <= _effective_persist_threshold_chars()):
        return None
    _ensure_dir()

    # Deterministic filename: hash of content so identical results are deduped.
    content_hash = hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()
    safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in (tool_call_id or "unknown"))
    owner = _owner_fingerprint(conversation_id, workspace_root)
    filename = (
        f"{safe_id}_{content_hash}_{owner}.txt"
        if owner
        else f"{safe_id}_{content_hash}.txt"
    )
    filepath = TOOL_RESULT_DATA_DIR / filename

    try:
        # Publish through the shared atomic writer. Identical content hashes
        # make concurrent writers harmless and avoid exposing partial files.
        with file_mutation_locks([filepath]):
            if not filepath.exists():
                try:
                    atomic_write_text(filepath, content, overwrite=False)
                except FileExistsError:
                    # Another process won the deterministic content-addressed
                    # publish. The hash in the filename guarantees the file is
                    # the same logical result, so reuse it.
                    pass
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
    *,
    conversation_id: str = "",
    workspace_root: str | Path | None = None,
) -> str:
    """Try to persist a tool result; return the preview or the original content.

    This is the main entry point for integration — if persistence succeeds,
    the preview (with disk reference) is returned; otherwise the original
    content is returned unchanged.
    """
    if not content or len(content) <= _effective_persist_threshold_chars():
        return content
    persisted = persist_tool_result(
        content,
        tool_call_id,
        tool_name,
        conversation_id=conversation_id,
        workspace_root=workspace_root,
    )
    if persisted is not None:
        return persisted.preview
    return content
