"""
Agent Loop Checkpoint System

Minimal checkpoint/resume for long-running tasks that fail mid-way
(timeout, network, interrupt). Saves state after each iteration to
`.claude/checkpoints/<session_id>/<timestamp>.json`.

Only stores serializable state: messages, tool_calls, iterations,
disabled_tools, active_skills. Complex runtime objects (workspace_context,
terminal_manager, permission_context) are NOT checkpointed — they must be
re-provided when resuming.

Usage:
  - Auto-checkpoint: loop.py calls save_checkpoint() after each iteration
  - Resume: /resume command or auto-detect incomplete checkpoint at loop start
"""

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


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


def get_checkpoint_dir(session_id: str, base_dir: Path | None = None) -> Path:
    """Returns .claude/checkpoints/<session_id>/"""
    if base_dir is None:
        base_dir = Path.home() / ".claude"
    checkpoint_dir = base_dir / "checkpoints" / session_id
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    return checkpoint_dir


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
    import time
    checkpoint = AgentCheckpoint(
        session_id=session_id,
        timestamp=time.time(),
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
    )
    checkpoint_dir = get_checkpoint_dir(session_id, base_dir)
    checkpoint_path = checkpoint_dir / f"{int(checkpoint.timestamp)}.json"
    with open(checkpoint_path, "w", encoding="utf-8") as f:
        json.dump(asdict(checkpoint), f, indent=2, ensure_ascii=False)
    logger.info(f"Checkpoint saved: {checkpoint_path}")
    return checkpoint_path


def save_run_checkpoint(**kwargs: Any) -> Path:
    """Explicit alias for run-resume checkpoints.

    Kept separate from backend.checkpoint.CheckpointManager, which snapshots
    files before writes and powers checkpoint.rewind.
    """
    return save_checkpoint(**kwargs)


def load_latest_checkpoint(session_id: str, base_dir: Path | None = None) -> AgentCheckpoint | None:
    """Load the latest incomplete checkpoint for this session."""
    checkpoint_dir = get_checkpoint_dir(session_id, base_dir)
    checkpoints = sorted(checkpoint_dir.glob("*.json"), reverse=True)
    if not checkpoints:
        return None

    latest = checkpoints[0]
    with open(latest, encoding="utf-8") as f:
        data = json.load(f)

    data.setdefault("run_id", "")
    data.setdefault("conversation_id", "")
    data.setdefault("checkpoint_type", "run_checkpoint")
    data.setdefault("resume_payload", {})
    checkpoint = AgentCheckpoint(**data)
    # Only resume if the task didn't complete naturally
    if checkpoint.stopped_reason in ("completed", None):
        return None
    return checkpoint


def load_latest_run_checkpoint(session_id: str, base_dir: Path | None = None) -> AgentCheckpoint | None:
    return load_latest_checkpoint(session_id, base_dir)


def clear_checkpoints(session_id: str, base_dir: Path | None = None) -> None:
    """Delete all checkpoints for this session."""
    checkpoint_dir = get_checkpoint_dir(session_id, base_dir)
    for cp in checkpoint_dir.glob("*.json"):
        cp.unlink()
    logger.info(f"Cleared checkpoints for session {session_id}")
