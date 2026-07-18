"""
Agent Loop Checkpoint System

Minimal checkpoint/resume for long-running tasks that fail mid-way
(timeout, network, interrupt). Saves state after each iteration to
`.claude/checkpoints/<session_id>/<timestamp_ms>-<seq>.json`.

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
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CHECKPOINT_SCHEMA_VERSION = 2
MAX_RETAINED_CHECKPOINTS = 12


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
    last_verified_mutation_index: int = 0
    run_id: str = ""
    conversation_id: str = ""
    checkpoint_type: str = "run_checkpoint"
    resume_payload: dict[str, Any] | None = None
    schema_version: int = CHECKPOINT_SCHEMA_VERSION
    sequence: int = 0
    checksum: str = ""


def get_checkpoint_dir(session_id: str, base_dir: Path | None = None) -> Path:
    """Returns .claude/checkpoints/<session_id>/"""
    if base_dir is None:
        base_dir = Path.home() / ".claude"
    checkpoint_dir = base_dir / "checkpoints" / session_id
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
    last_verified_mutation_index: int,
    base_dir: Path | None = None,
    run_id: str = "",
    conversation_id: str = "",
    resume_payload: dict[str, Any] | None = None,
) -> Path:
    """Save checkpoint after each iteration."""
    checkpoint_dir = get_checkpoint_dir(session_id, base_dir)
    sequence = _next_sequence(checkpoint_dir)
    now = time.time()
    checkpoint = AgentCheckpoint(
        session_id=session_id,
        timestamp=now,
        user_message=user_message,
        iterations=iterations,
        reply=reply,
        messages=messages,
        tool_calls=[asdict(tc) for tc in tool_calls],
        active_skills=active_skills,
        disabled_tools=list(disabled_tools),
        stopped_reason=stopped_reason,
        last_mutation_index=last_mutation_index,
        last_verified_mutation_index=last_verified_mutation_index,
        run_id=run_id,
        conversation_id=conversation_id,
        checkpoint_type="run_checkpoint",
        resume_payload=resume_payload or {},
        schema_version=CHECKPOINT_SCHEMA_VERSION,
        sequence=sequence,
    )
    payload = asdict(checkpoint)
    payload["checksum"] = _compute_checksum(payload)
    checkpoint.checksum = payload["checksum"]

    ts_ms = int(now * 1000)
    checkpoint_path = checkpoint_dir / f"{ts_ms}-{sequence:06d}.json"
    temp_path = checkpoint_path.with_suffix(".json.tmp")
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
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
    known = {field.name for field in AgentCheckpoint.__dataclass_fields__.values()}  # type: ignore[attr-defined]
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


def load_latest_checkpoint(session_id: str, base_dir: Path | None = None) -> AgentCheckpoint | None:
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
            if checkpoint.stopped_reason not in ("completed", None):
                return checkpoint
        except Exception as exc:
            logger.warning("Skipping unreadable checkpoint %s: %s", path, exc)
    return None


def load_latest_run_checkpoint(session_id: str, base_dir: Path | None = None) -> AgentCheckpoint | None:
    return load_latest_checkpoint(session_id, base_dir)


def clear_checkpoints(session_id: str, base_dir: Path | None = None) -> None:
    """Delete all checkpoints for this session."""
    checkpoint_dir = get_checkpoint_dir(session_id, base_dir)
    for cp in checkpoint_dir.glob("*.json"):
        cp.unlink()
    logger.info(f"Cleared checkpoints for session {session_id}")
