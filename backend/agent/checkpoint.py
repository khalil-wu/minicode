"""
Agent Loop Checkpoint System

Minimal checkpoint/resume for long-running tasks that fail mid-way
(timeout, network, interrupt). Desktop/runtime state is stored below
``MINICODE_STATE_ROOT`` when configured; CLI compatibility falls back to
``~/.claude``.

Only stores serializable state: messages, tool_calls, iterations,
disabled_tools, active_skills. Complex runtime objects (workspace_context,
terminal_manager, permission_context) are NOT checkpointed — they must be
re-provided when resuming.

Usage:
  - Auto-checkpoint: loop.py calls save_checkpoint() after each iteration
  - Resume: /resume command or auto-detect incomplete checkpoint at loop start
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, fields, is_dataclass
from itertools import islice
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CHECKPOINT_SCHEMA_VERSION = 2
MAX_RETAINED_CHECKPOINTS = 12
MAX_STORAGE_ID_LENGTH = 128
MAX_CHECKPOINT_HISTORY_MESSAGES = 160
MAX_CHECKPOINT_TOOL_CALLS = 200
MAX_CHECKPOINT_TEXT_CHARS = 32 * 1024
MAX_CHECKPOINT_COLLECTION_ITEMS = 256
MAX_CHECKPOINT_NESTING = 8
MAX_CHECKPOINT_BYTES = 2 * 1024 * 1024
_STORAGE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


def _bounded_checkpoint_text(value: str) -> str:
    if len(value) <= MAX_CHECKPOINT_TEXT_CHARS:
        return value
    head = MAX_CHECKPOINT_TEXT_CHARS * 3 // 4
    tail = MAX_CHECKPOINT_TEXT_CHARS - head
    return f"{value[:head]}\n...[checkpoint text truncated]...\n{value[-tail:]}"


@dataclass
class _CheckpointByteBudget:
    remaining: int

    def reserve(self, value: Any) -> bool:
        encoded_size = len(
            json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        return self.reserve_bytes(encoded_size)

    def reserve_bytes(self, size: int) -> bool:
        if size < 0 or size > self.remaining:
            return False
        self.remaining -= size
        return True


_CHECKPOINT_OMITTED = object()
_CHECKPOINT_DYNAMIC_FIELDS = (
    "session_id",
    "user_message",
    "reply",
    "messages",
    "tool_calls",
    "resume_payload",
    "active_skills",
    "disabled_tools",
    "stopped_reason",
    "run_id",
    "conversation_id",
)


def _budgeted_checkpoint_text(value: str, budget: _CheckpointByteBudget) -> Any:
    candidate = _bounded_checkpoint_text(value)
    if budget.reserve(candidate):
        return candidate

    marker = "...[checkpoint text truncated]..."
    if not budget.reserve(marker):
        return _CHECKPOINT_OMITTED

    # Reclaim the marker reservation while finding the longest UTF-8 prefix
    # that fits beside it. The candidate is already bounded by the per-value
    # text contract, so this search never materializes the original huge text.
    marker_size = len(json.dumps(marker, ensure_ascii=False).encode("utf-8"))
    budget.remaining += marker_size
    low = 0
    high = len(candidate)
    best = marker
    while low <= high:
        midpoint = (low + high) // 2
        proposed = candidate[:midpoint] + marker
        proposed_size = len(json.dumps(proposed, ensure_ascii=False).encode("utf-8"))
        if proposed_size <= budget.remaining:
            best = proposed
            low = midpoint + 1
        else:
            high = midpoint - 1
    if not budget.reserve(best):
        return _CHECKPOINT_OMITTED
    return best


def _bounded_checkpoint_value(
    value: Any,
    *,
    depth: int = 0,
    budget: _CheckpointByteBudget | None = None,
) -> Any:
    if budget is None:
        budget = _CheckpointByteBudget(MAX_CHECKPOINT_BYTES)
    if depth >= MAX_CHECKPOINT_NESTING:
        marker = "[checkpoint value omitted: nesting limit]"
        return marker if budget.reserve(marker) else _CHECKPOINT_OMITTED
    if isinstance(value, str):
        return _budgeted_checkpoint_text(value, budget)
    if is_dataclass(value) and not isinstance(value, type):
        value = {field.name: getattr(value, field.name) for field in fields(value)}
    if isinstance(value, (list, tuple)):
        if not budget.reserve_bytes(2):
            return _CHECKPOINT_OMITTED
        bounded_reversed: list[Any] = []
        for item in reversed(value[-MAX_CHECKPOINT_COLLECTION_ITEMS:]):
            before_item = budget.remaining
            if bounded_reversed and not budget.reserve_bytes(1):
                break
            bounded = _bounded_checkpoint_value(item, depth=depth + 1, budget=budget)
            if bounded is _CHECKPOINT_OMITTED:
                budget.remaining = before_item
                break
            bounded_reversed.append(bounded)
        bounded_reversed.reverse()
        return bounded_reversed
    if isinstance(value, dict):
        if not budget.reserve_bytes(2):
            return _CHECKPOINT_OMITTED
        bounded_reversed: list[tuple[str, Any]] = []
        for key, item in islice(reversed(value.items()), MAX_CHECKPOINT_COLLECTION_ITEMS):
            before_item = budget.remaining
            key_text = str(key)
            key_size = len(json.dumps(key_text, ensure_ascii=False).encode("utf-8")) + 1
            separator_size = 1 if bounded_reversed else 0
            if not budget.reserve_bytes(key_size + separator_size):
                break
            bounded = _bounded_checkpoint_value(item, depth=depth + 1, budget=budget)
            if bounded is _CHECKPOINT_OMITTED:
                budget.remaining = before_item
                break
            bounded_reversed.append((key_text, bounded))
        bounded_reversed.reverse()
        return dict(bounded_reversed)
    if value is None or isinstance(value, (bool, int, float)):
        return value if budget.reserve(value) else _CHECKPOINT_OMITTED
    return _budgeted_checkpoint_text(str(value), budget)


def _fit_checkpoint_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Bound traversal and final JSON with one shared checkpoint byte budget."""
    fitted = dict(payload)
    original_dynamic = {field: fitted.get(field) for field in _CHECKPOINT_DYNAMIC_FIELDS}
    for field in _CHECKPOINT_DYNAMIC_FIELDS:
        value = original_dynamic[field]
        fitted[field] = [] if isinstance(value, (list, tuple, set)) else ({} if isinstance(value, dict) else "")

    # The final checksum is always a 64-character SHA-256 hex digest. Reserving
    # its exact wire shape here makes the limit authoritative after replacement.
    fitted["checksum"] = "0" * 64
    fixed_size = len(json.dumps(fitted, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    budget = _CheckpointByteBudget(max(0, MAX_CHECKPOINT_BYTES - fixed_size))

    for field in _CHECKPOINT_DYNAMIC_FIELDS:
        value = original_dynamic[field]
        if field == "messages" and isinstance(value, (list, tuple)):
            value = value[-MAX_CHECKPOINT_HISTORY_MESSAGES:]
        elif field == "tool_calls" and isinstance(value, (list, tuple)):
            value = value[-MAX_CHECKPOINT_TOOL_CALLS:]
        elif isinstance(value, set):
            value = sorted(value, key=str)
        bounded = _bounded_checkpoint_value(value, budget=budget)
        if bounded is not _CHECKPOINT_OMITTED:
            fitted[field] = bounded

    encoded = json.dumps(fitted, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) <= MAX_CHECKPOINT_BYTES:
        return fitted

    # Conservative accounting above should make this unreachable. Keep a
    # schema-valid minimal payload as the final invariant, even for malformed
    # direct callers with pathological scalar metadata.
    for field in ("resume_payload", "messages", "tool_calls", "active_skills", "disabled_tools"):
        fitted[field] = {} if field == "resume_payload" else []
    fitted["user_message"] = "[checkpoint content omitted: size limit]"
    fitted["reply"] = ""
    return fitted


@dataclass
class AgentCheckpoint:
    """Serializable agent state snapshot."""
    session_id: str
    timestamp: float
    user_message: str
    iterations: int
    reply: str
    messages: list[dict[str, Any]]  # ContextBuilder.messages history
    tool_calls: list[dict[str, Any]]  # AgentState.tool_calls serialized
    active_skills: list[str]
    disabled_tools: list[str]
    stopped_reason: str | None = None
    last_mutation_index: int = 0
    run_id: str = ""
    conversation_id: str = ""
    checkpoint_type: str = "run_checkpoint"
    resume_payload: dict[str, Any] | None = None
    schema_version: int = CHECKPOINT_SCHEMA_VERSION
    sequence: int = 0
    checksum: str = ""


def validate_storage_id(value: str, *, field_name: str = "storage_id") -> str:
    """Validate an identifier before using it as a storage directory name."""
    clean = str(value or "").strip()
    if not clean:
        raise ValueError(f"{field_name} is required")
    if len(clean) > MAX_STORAGE_ID_LENGTH:
        raise ValueError(f"{field_name} is too long")
    if clean in {".", ".."} or not _STORAGE_ID_RE.fullmatch(clean):
        raise ValueError(f"Invalid {field_name}")
    if clean.endswith(".") or clean.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES:
        raise ValueError(f"Invalid {field_name}")
    return clean


def get_checkpoint_dir(session_id: str, base_dir: Path | None = None) -> Path:
    """Return the isolated run-checkpoint directory for a session."""
    clean_session_id = validate_storage_id(session_id, field_name="session_id")
    if base_dir is None:
        state_root = str(os.environ.get("MINICODE_STATE_ROOT") or "").strip()
        base_dir = (
            Path(state_root).expanduser().resolve() / "data" / "agent-runtime"
            if state_root
            else Path.home() / ".claude"
        )
    checkpoint_dir = base_dir / "checkpoints" / clean_session_id
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    return checkpoint_dir


def _payload_for_checksum(data: dict[str, Any]) -> dict[str, Any]:
    payload = dict(data)
    payload.pop("checksum", None)
    return payload


def _compute_checksum(data: dict[str, Any]) -> str:
    raw = json.dumps(
        _payload_for_checksum(data),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _next_sequence(checkpoint_dir: Path) -> int:
    highest = 0
    for path in checkpoint_dir.glob("*.json"):
        stem = path.stem
        # Prefer trailing -<seq> when present.
        if "-" in stem:
            tail = stem.rsplit("-", 1)[-1]
            if tail.isdigit():
                highest = max(highest, int(tail))
                continue
        if stem.isdigit():
            highest = max(highest, int(stem))
    return highest + 1


def _prune_checkpoints(checkpoint_dir: Path, *, keep: int = MAX_RETAINED_CHECKPOINTS) -> None:
    paths = sorted(
        [path for path in checkpoint_dir.glob("*.json") if path.is_file()],
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    for stale in paths[max(0, keep):]:
        try:
            stale.unlink()
        except OSError as exc:
            logger.debug("Failed pruning checkpoint %s: %s", stale, exc)


def save_checkpoint(
    session_id: str,
    user_message: str,
    iterations: int,
    reply: str,
    messages: list[dict[str, Any]],
    tool_calls: list[Any],  # list[ToolCallRecord]
    active_skills: list[str],
    disabled_tools: set[str],
    stopped_reason: str | None,
    last_mutation_index: int,
    base_dir: Path | None = None,
    run_id: str = "",
    conversation_id: str = "",
    resume_payload: dict[str, Any] | None = None,
) -> Path:
    """Save checkpoint after each iteration."""
    checkpoint_dir = get_checkpoint_dir(session_id, base_dir)
    sequence = _next_sequence(checkpoint_dir)
    now = time.time()
    payload = _fit_checkpoint_payload({
        "session_id": session_id,
        "timestamp": now,
        "user_message": str(user_message or ""),
        "iterations": iterations,
        "reply": str(reply or ""),
        "messages": messages,
        "tool_calls": tool_calls,
        "active_skills": active_skills,
        "disabled_tools": disabled_tools,
        "stopped_reason": stopped_reason,
        "last_mutation_index": last_mutation_index,
        "run_id": run_id,
        "conversation_id": conversation_id,
        "checkpoint_type": "run_checkpoint",
        "resume_payload": resume_payload or {},
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "sequence": sequence,
        "checksum": "",
    })
    payload["checksum"] = _compute_checksum(payload)

    ts_ms = int(now * 1000)
    checkpoint_path = checkpoint_dir / f"{ts_ms}-{sequence:06d}.json"
    temp_path = checkpoint_path.with_suffix(".json.tmp")
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
        f.flush()
        os.fsync(f.fileno())
    temp_path.replace(checkpoint_path)
    _prune_checkpoints(checkpoint_dir)
    logger.info(f"Checkpoint saved: {checkpoint_path}")
    return checkpoint_path


def save_run_checkpoint(**kwargs: Any) -> Path:
    """Explicit alias for run-resume checkpoints.

    Kept separate from backend.checkpoint.CheckpointManager, which snapshots
    files before writes and powers checkpoint.rewind.
    """
    return save_checkpoint(**kwargs)


def _checkpoint_from_dict(data: dict[str, Any]) -> AgentCheckpoint:
    data = dict(data)
    data.setdefault("run_id", "")
    data.setdefault("conversation_id", "")
    data.setdefault("checkpoint_type", "run_checkpoint")
    data.setdefault("resume_payload", {})
    data.setdefault("schema_version", 1)
    data.setdefault("sequence", 0)
    data.setdefault("checksum", "")
    # Drop unknown fields so older/newer files still load.
    known = {item.name for item in AgentCheckpoint.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    filtered = {key: value for key, value in data.items() if key in known}
    return AgentCheckpoint(**filtered)


def _verify_checkpoint_payload(data: dict[str, Any], path: Path) -> bool:
    checksum = str(data.get("checksum") or "").strip()
    if not checksum:
        # Legacy second-granularity checkpoints without checksum remain loadable.
        return True
    expected = _compute_checksum(data)
    if checksum != expected:
        logger.warning("Skipping checkpoint with bad checksum %s", path)
        return False
    return True


def load_latest_checkpoint(
    session_id: str,
    base_dir: Path | None = None,
    *,
    conversation_id: str | None = None,
) -> AgentCheckpoint | None:
    """Load the latest incomplete checkpoint for this session."""
    checkpoint_dir = get_checkpoint_dir(session_id, base_dir)
    checkpoints = sorted(checkpoint_dir.glob("*.json"), reverse=True)
    if not checkpoints:
        return None

    for path in checkpoints:
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                logger.warning("Skipping non-object checkpoint %s", path)
                continue
            if not _verify_checkpoint_payload(data, path):
                continue
            checkpoint = _checkpoint_from_dict(data)
            if conversation_id is not None and checkpoint.conversation_id != conversation_id:
                continue
            if checkpoint.stopped_reason not in ("completed", None):
                return checkpoint
        except Exception as exc:
            logger.warning("Skipping unreadable checkpoint %s: %s", path, exc)
    return None


def load_latest_run_checkpoint(
    session_id: str,
    base_dir: Path | None = None,
    *,
    conversation_id: str | None = None,
) -> AgentCheckpoint | None:
    return load_latest_checkpoint(
        session_id,
        base_dir,
        conversation_id=conversation_id,
    )


def clear_checkpoints(
    session_id: str,
    base_dir: Path | None = None,
    *,
    conversation_id: str | None = None,
) -> None:
    """Delete checkpoints for this session, optionally scoped to a conversation."""
    checkpoint_dir = get_checkpoint_dir(session_id, base_dir)
    for cp in checkpoint_dir.glob("*.json"):
        if conversation_id is not None:
            try:
                with cp.open(encoding="utf-8") as handle:
                    data = json.load(handle)
                if not isinstance(data, dict) or str(data.get("conversation_id") or "") != conversation_id:
                    continue
            except (OSError, json.JSONDecodeError):
                continue
        cp.unlink()
    logger.info(
        "Cleared checkpoints for session %s conversation %s",
        session_id,
        conversation_id or "*",
    )
