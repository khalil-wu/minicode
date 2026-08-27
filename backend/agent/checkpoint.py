"""
Agent Loop Checkpoint System

Minimal checkpoint/resume for long-running tasks that fail mid-way
(timeout, network, interrupt). Desktop/runtime state is stored below
``MINICODE_STATE_ROOT`` when configured and otherwise below
``~/.minicode/data/agent-runtime``.

Only stores serializable state: messages, tool_calls, iterations,
disabled_tools, loaded_deferred_tools, active_skills. Complex runtime objects (workspace_context,
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
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, fields, is_dataclass
from itertools import islice
from pathlib import Path
from typing import Any

from filelock import FileLock

from backend.atomic_io import atomic_write_text, canonical_file_path_key

logger = logging.getLogger(__name__)


class CheckpointError(RuntimeError):
    """Base error for durable run-checkpoint operations."""


class CheckpointCorruptionError(CheckpointError):
    """Raised when checkpoint bytes cannot be trusted for recovery."""


CHECKPOINT_SCHEMA_VERSION = 4
# The context payload has its own schema so the checkpoint envelope can evolve
# independently from ContextBuilder's internal implementation.  A checkpoint
# with no ``context_snapshot`` remains a valid legacy (history-only) payload.
CONTEXT_SNAPSHOT_SCHEMA_VERSION = 1
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

_CHECKPOINT_THREAD_LOCK_GUARD = threading.Lock()
_CHECKPOINT_THREAD_LOCKS: dict[str, threading.RLock] = {}


@contextmanager
def _checkpoint_write_lock(checkpoint_dir: Path):
    """Serialize sequence allocation and publication for one session.

    Run checkpoints are written by independent conversation tasks that share
    one session directory.  A timestamp plus an unlocked directory scan lets
    two writers select the same sequence and overwrite one another.  Keep the
    lock file beside the session checkpoints so separate backend processes
    observe the same fence, while the in-process RLock avoids unnecessary
    file-lock churn for the common case.
    """

    key = canonical_file_path_key(checkpoint_dir)
    with _CHECKPOINT_THREAD_LOCK_GUARD:
        thread_lock = _CHECKPOINT_THREAD_LOCKS.get(key)
        if thread_lock is None:
            thread_lock = threading.RLock()
            _CHECKPOINT_THREAD_LOCKS[key] = thread_lock
    with thread_lock:
        # A bounded wait, matching every other cross-process lock in the repo
        # (execution_journal, parent_notification_outbox, atomic_io). The
        # cancellation and terminal paths take this lock synchronously on the
        # event loop, so an unbounded wait would stall the loop, the drain
        # budget and the WebSocket pump. turn_kernel projects the resulting
        # failure as save_failed / clear_failed.
        with FileLock(str(checkpoint_dir / ".checkpoints.lock"), timeout=60):
            yield


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
        # Measure by streaming. A value far larger than the budget must be
        # rejected without ever materializing its full JSON encoding: the
        # measurement itself would otherwise be the thing that runs out of
        # memory, before any bound could be applied.
        encoder = json.JSONEncoder(ensure_ascii=False, separators=(",", ":"))
        encoded_size = 0
        for chunk in encoder.iterencode(value):
            encoded_size += len(chunk.encode("utf-8"))
            if encoded_size > self.remaining:
                return False
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
    "context_snapshot",
    "messages",
    "tool_calls",
    "resume_payload",
    "active_skills",
    "disabled_tools",
    "loaded_deferred_tools",
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


def _fit_checkpoint_payload(
    payload: dict[str, Any],
    *,
    authoritative_snapshot: bool = True,
) -> dict[str, Any]:
    """Bound traversal and final JSON with one shared checkpoint byte budget.

    ``authoritative_snapshot`` marks a caller-supplied context snapshot: that
    structure is the provider replay authority and may only fit whole or fail.
    A snapshot synthesized here from ``messages`` carries no such authority and
    is bounded by the same rules as the ``messages`` field itself.
    """
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
        # ContextBuilder's snapshot is the authoritative provider replay
        # structure.  Never recursively truncate encrypted reasoning,
        # signatures, tool arguments, or message content here.  It either
        # fits as one JSON value or is omitted as a whole so recovery can
        # reject the incomplete schema-4 checkpoint instead of accepting a
        # syntactically valid but semantically corrupted continuation.
        if field == "context_snapshot":
            if authoritative_snapshot:
                bounded = value if budget.reserve(value) else _CHECKPOINT_OMITTED
            else:
                bounded = _bounded_checkpoint_value(value, budget=budget)
        else:
            bounded = _bounded_checkpoint_value(value, budget=budget)
        if bounded is not _CHECKPOINT_OMITTED:
            fitted[field] = bounded

    if (
        authoritative_snapshot
        and isinstance(original_dynamic.get("context_snapshot"), dict)
        and original_dynamic["context_snapshot"]
        and fitted.get("context_snapshot") != original_dynamic["context_snapshot"]
    ):
        raise ValueError(
            "authoritative context snapshot exceeds checkpoint byte budget"
        )

    encoded = json.dumps(fitted, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) <= MAX_CHECKPOINT_BYTES:
        return fitted

    # Conservative accounting above should make this unreachable. A context
    # snapshot that reaches this branch must never be replaced by a schema-
    # valid-looking empty object, because that would publish a false replay
    # boundary.
    if isinstance(original_dynamic.get("context_snapshot"), dict) and original_dynamic["context_snapshot"]:
        raise ValueError("authoritative context snapshot exceeds checkpoint byte budget")

    # Keep a schema-valid minimal payload as the final invariant for malformed
    # direct callers with pathological non-context metadata.
    for field in (
        "resume_payload",
        "context_snapshot",
        "messages",
        "tool_calls",
        "active_skills",
        "disabled_tools",
        "loaded_deferred_tools",
    ):
        fitted[field] = (
            {}
            if field in {"resume_payload", "context_snapshot"}
            else []
        )
    fitted["user_message"] = "[checkpoint content omitted: size limit]"
    fitted["reply"] = ""
    return fitted


def context_snapshot_revision(snapshot: dict[str, Any] | None) -> str:
    """Return the revision of the exact snapshot payload being persisted."""

    if not isinstance(snapshot, dict) or not snapshot:
        return ""
    canonical_snapshot = dict(snapshot)
    try:
        canonical_snapshot["context_schema_version"] = int(
            canonical_snapshot.get("context_schema_version")
            or CONTEXT_SNAPSHOT_SCHEMA_VERSION
        )
    except (TypeError, ValueError):
        canonical_snapshot["context_schema_version"] = CONTEXT_SNAPSHOT_SCHEMA_VERSION
    canonical_snapshot.pop("context_revision", None)
    canonical = json.dumps(
        canonical_snapshot,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _refresh_context_snapshot_revision(payload: dict[str, Any]) -> None:
    """Stamp the bounded snapshot with a deterministic durable revision.

    The in-memory context may have been bounded by the checkpoint byte budget;
    therefore a revision calculated before persistence could describe bytes
    that are no longer present on disk.  Recompute it after bounding and before
    the envelope checksum so recovery can prove exactly which snapshot it
    loaded.
    """

    snapshot = payload.get("context_snapshot")
    if not isinstance(snapshot, dict) or not snapshot:
        payload["context_revision"] = ""
        return
    snapshot = dict(snapshot)
    snapshot["context_schema_version"] = int(
        snapshot.get("context_schema_version")
        or CONTEXT_SNAPSHOT_SCHEMA_VERSION
    )
    snapshot["context_revision"] = context_snapshot_revision(snapshot)
    payload["context_snapshot"] = snapshot
    payload["context_revision"] = snapshot["context_revision"]


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
    loaded_deferred_tools: list[str] = field(default_factory=list)
    stopped_reason: str | None = None
    last_mutation_index: int = 0
    run_id: str = ""
    conversation_id: str = ""
    checkpoint_type: str = "run_checkpoint"
    resume_payload: dict[str, Any] | None = None
    # Canonical ContextBuilder snapshot.  ``messages`` remains as a legacy
    # compatibility projection for schema <= 3 callers and files.
    context_snapshot: dict[str, Any] = field(default_factory=dict)
    context_revision: str = ""
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
        from backend.runtime_paths import agent_runtime_root

        base_dir = agent_runtime_root()
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
    failures: list[str] = []
    for stale in paths[max(0, keep):]:
        try:
            stale.unlink()
        except OSError as exc:
            failures.append(f"{stale.name}: {type(exc).__name__}: {exc}")
    if failures:
        raise RuntimeError(
            "Checkpoint cleanup failed: " + "; ".join(failures)
        )


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
    loaded_deferred_tools: set[str] | None = None,
    base_dir: Path | None = None,
    run_id: str = "",
    conversation_id: str = "",
    resume_payload: dict[str, Any] | None = None,
    context_snapshot: dict[str, Any] | None = None,
) -> Path:
    """Save checkpoint after each iteration."""
    checkpoint_dir = get_checkpoint_dir(session_id, base_dir)
    with _checkpoint_write_lock(checkpoint_dir):
        sequence = _next_sequence(checkpoint_dir)
        now = time.time()
        authoritative_snapshot = isinstance(context_snapshot, dict) and bool(
            context_snapshot
        )
        raw_context_snapshot = (
            dict(context_snapshot)
            if authoritative_snapshot
            # No caller-supplied replay structure: project one from the bounded
            # message tail, the same window the ``messages`` field keeps.
            else {"history": list(messages or [])[-MAX_CHECKPOINT_HISTORY_MESSAGES:]}
        )
        try:
            context_schema_version = int(
                raw_context_snapshot.get(
                    "context_schema_version",
                    CONTEXT_SNAPSHOT_SCHEMA_VERSION,
                )
            )
        except (TypeError, ValueError):
            context_schema_version = CONTEXT_SNAPSHOT_SCHEMA_VERSION
        ordered_context_snapshot = {
            key: value
            for key, value in raw_context_snapshot.items()
            if key
            not in {
                "history",
                "context_schema_version",
                "context_revision",
            }
        }
        if raw_context_snapshot:
            # The generic bounded-dict encoder retains values from the end.
            # Put canonical history and its schema fence last so a snapshot
            # never keeps auxiliary metadata while silently dropping the
            # transcript that metadata describes.
            ordered_context_snapshot["history"] = raw_context_snapshot.get(
                "history", messages
            )
            ordered_context_snapshot["context_schema_version"] = (
                context_schema_version
            )
            ordered_context_snapshot["context_revision"] = "0" * 64
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
            "loaded_deferred_tools": loaded_deferred_tools or set(),
            "stopped_reason": stopped_reason,
            "last_mutation_index": last_mutation_index,
            "run_id": run_id,
            "conversation_id": conversation_id,
            "checkpoint_type": "run_checkpoint",
            "resume_payload": resume_payload or {},
            "context_snapshot": ordered_context_snapshot,
            "context_revision": "0" * 64 if raw_context_snapshot else "",
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "sequence": sequence,
            "checksum": "",
        }, authoritative_snapshot=authoritative_snapshot)
        _refresh_context_snapshot_revision(payload)
        payload["checksum"] = _compute_checksum(payload)

        ts_ms = int(now * 1000)
        checkpoint_path = checkpoint_dir / f"{ts_ms}-{sequence:06d}.json"
        atomic_write_text(
            checkpoint_path,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        )
        _prune_checkpoints(checkpoint_dir)
    logger.info(f"Checkpoint saved: {checkpoint_path}")
    return checkpoint_path


def save_run_checkpoint(
    *,
    receipt: dict[str, Any] | None = None,
    **kwargs: Any,
) -> Path:
    """Explicit alias for run-resume checkpoints.

    Kept separate from backend.checkpoint.CheckpointManager, which snapshots
    files before writes and powers checkpoint.rewind.
    """
    path = save_checkpoint(**kwargs)
    if isinstance(receipt, dict):
        # The receipt reflects the post-bounding bytes, not the pre-save
        # in-memory candidate.  Failure to read our just-published file is a
        # checkpoint failure and must not produce a false ``saved`` result.
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or not _verify_checkpoint_payload(
            payload, path
        ):
            raise RuntimeError("Published checkpoint failed receipt verification")
        receipt.update(
            {
                "path": str(path),
                "schema_version": int(payload.get("schema_version") or 1),
                "sequence": int(payload.get("sequence") or 0),
                "context_revision": str(
                    payload.get("context_revision") or ""
                ),
            }
        )
    return path


def _checkpoint_from_dict(data: dict[str, Any]) -> AgentCheckpoint:
    data = dict(data)
    known = {item.name for item in AgentCheckpoint.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    unknown = sorted(set(data) - known)
    if unknown:
        raise CheckpointCorruptionError(
            "Checkpoint contains unknown fields: " + ", ".join(unknown)
        )
    return AgentCheckpoint(**data)


def _verify_checkpoint_payload(data: dict[str, Any], path: Path) -> bool:
    checksum = str(data.get("checksum") or "").strip()
    if not checksum:
        logger.warning("Rejecting checkpoint without checksum %s", path)
        return False
    expected = _compute_checksum(data)
    if checksum != expected:
        logger.warning("Rejecting checkpoint with bad checksum %s", path)
        return False
    snapshot = data.get("context_snapshot")
    try:
        schema_version = int(data.get("schema_version") or 0)
    except (TypeError, ValueError):
        logger.warning("Rejecting checkpoint with invalid schema version %s", path)
        return False
    if schema_version != CHECKPOINT_SCHEMA_VERSION:
        logger.warning(
            "Rejecting checkpoint with unsupported schema version %s: %s",
            path,
            schema_version,
        )
        return False
    if not (
        isinstance(snapshot, dict) and snapshot
    ):
        logger.warning("Rejecting schema-4 checkpoint without context snapshot %s", path)
        return False
    if isinstance(snapshot, dict) and snapshot:
        expected_revision = str(
            data.get("context_revision")
            or snapshot.get("context_revision")
            or ""
        ).strip()
        actual_revision = context_snapshot_revision(snapshot)
        if expected_revision and expected_revision != actual_revision:
            logger.warning("Rejecting checkpoint with bad context revision %s", path)
            return False
        if not expected_revision:
            logger.warning("Rejecting schema-4 checkpoint without context revision %s", path)
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
                raise CheckpointCorruptionError(
                    f"Checkpoint payload is not an object: {path.name}"
                )
            # Ownership before integrity: checkpoint directories are
            # session-scoped and shared by every conversation in that session,
            # so a corrupt checkpoint owned by another conversation must not
            # abort this one's resume. clear_checkpoints filters the same way.
            if (
                conversation_id is not None
                and str(data.get("conversation_id") or "") != conversation_id
            ):
                continue
            if not _verify_checkpoint_payload(data, path):
                raise CheckpointCorruptionError(
                    f"Checkpoint verification failed: {path.name}"
                )
            checkpoint = _checkpoint_from_dict(data)
            if checkpoint.stopped_reason == "completed":
                return None
            if checkpoint.stopped_reason not in ("completed", None):
                return checkpoint
        except CheckpointCorruptionError:
            raise
        except Exception as exc:
            raise CheckpointCorruptionError(
                f"Checkpoint is unreadable: {path.name}"
            ) from exc
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
    with _checkpoint_write_lock(checkpoint_dir):
        for cp in checkpoint_dir.glob("*.json"):
            if conversation_id is not None:
                try:
                    with cp.open(encoding="utf-8") as handle:
                        data = json.load(handle)
                    if not isinstance(data, dict):
                        raise ValueError("checkpoint payload is not an object")
                    if str(data.get("conversation_id") or "") != conversation_id:
                        continue
                except (OSError, ValueError, json.JSONDecodeError) as exc:
                    # A scoped clear cannot claim success while an unreadable
                    # file remains in the same session directory: it may be
                    # the exact stale checkpoint that would be resumed later.
                    raise RuntimeError(
                        f"Cannot verify checkpoint ownership before clear: {cp.name}"
                    ) from exc
            cp.unlink(missing_ok=True)
    logger.info(
        "Cleared checkpoints for session %s conversation %s",
        session_id,
        conversation_id or "*",
    )


def clear_checkpoints_for_conversation(
    conversation_id: str,
    base_dir: Path | None = None,
) -> int:
    """Delete one conversation's run checkpoints across websocket sessions."""

    owner = validate_storage_id(conversation_id, field_name="conversation_id")
    if base_dir is None:
        from backend.runtime_paths import agent_runtime_root

        base_dir = agent_runtime_root()
    checkpoint_root = Path(base_dir).resolve() / "checkpoints"
    if not checkpoint_root.is_dir():
        return 0
    removed = 0
    for session_dir in checkpoint_root.iterdir():
        if not session_dir.is_dir():
            continue
        with _checkpoint_write_lock(session_dir):
            for checkpoint_path in session_dir.glob("*.json"):
                try:
                    payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    # A conversation-scoped delete cannot claim completion
                    # while an unreadable record remains in the shared session
                    # directory.  It may still be the recovery boundary for
                    # this conversation, so preserve the uncertainty.
                    raise RuntimeError(
                        f"Cannot verify checkpoint ownership before clear: {checkpoint_path.name}"
                    ) from exc
                if not isinstance(payload, dict) or str(payload.get("conversation_id") or "") != owner:
                    continue
                try:
                    checkpoint_path.unlink(missing_ok=True)
                except OSError as exc:
                    raise RuntimeError(
                        f"Failed to clear checkpoint {checkpoint_path.name}"
                    ) from exc
                removed += 1
            try:
                session_dir.rmdir()
            except OSError:
                pass
    logger.info("Cleared %d checkpoints for conversation %s", removed, owner)
    return removed
