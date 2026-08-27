"""Checkpoint restoration owned by QueryEngine before loop execution."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any

from backend.agent.checkpoint import (
    AgentCheckpoint,
    CONTEXT_SNAPSHOT_SCHEMA_VERSION,
    context_snapshot_revision,
    load_latest_checkpoint,
)
from backend.agent.message import AgentEvent
from backend.agent.state import AgentState, ToolCallRecord


logger = logging.getLogger(__name__)

_RESERVED_RESUME_KEYS = {
    "run_id",
    "conversation_id",
    "agent_runtime",
    "parent_run_id",
    "session_id",
    "minicode_session_id",
    "task_id",
}


@dataclass(frozen=True, slots=True)
class QueryRecoveryResult:
    checkpoint: AgentCheckpoint | None = None
    startup_events: tuple[AgentEvent, ...] = ()

    @property
    def restored(self) -> bool:
        return self.checkpoint is not None


def prepare_query_recovery(
    *,
    session_id: str,
    conversation_id: str,
    metadata: dict[str, Any],
    state: AgentState,
    context_builder: Any,
    max_iterations_budget: int,
    current_run_id: str,
    skill_manager: Any | None = None,
) -> QueryRecoveryResult:
    """Restore one incomplete checkpoint without taking over run identity.

    The newly fenced QueryEngine run remains authoritative. The old run and
    checkpoint identifiers are retained only as provenance for diagnostics.
    """

    metadata["_query_engine_recovery_prepared"] = True
    if not metadata.get("resume_from_checkpoint") or not session_id or not conversation_id:
        return QueryRecoveryResult()

    checkpoint = load_latest_checkpoint(session_id, conversation_id=conversation_id)
    if checkpoint is None:
        return QueryRecoveryResult()

    logger.info(
        "QueryEngine restoring checkpoint: session=%s conversation=%s iterations=%s",
        session_id,
        conversation_id,
        checkpoint.iterations,
    )
    state.iterations = checkpoint.iterations
    state.max_iterations = max(
        state.max_iterations,
        checkpoint.iterations + max_iterations_budget,
    )
    state.reply = checkpoint.reply
    state.active_skills = list(checkpoint.active_skills)
    state.disabled_tools = set(checkpoint.disabled_tools)
    state.loaded_deferred_tools = set(checkpoint.loaded_deferred_tools)
    state._last_mutation_index = checkpoint.last_mutation_index
    state.tool_calls = [ToolCallRecord(**record) for record in checkpoint.tool_calls]
    state.rebuild_tool_call_accounting()
    raw_snapshot = checkpoint.context_snapshot
    has_context_snapshot = isinstance(raw_snapshot, dict) and bool(raw_snapshot)
    if has_context_snapshot:
        snapshot = dict(raw_snapshot)
        expected_revision = str(
            checkpoint.context_revision
            or snapshot.get("context_revision")
            or ""
        ).strip()
        actual_revision = context_snapshot_revision(snapshot)
        # A schema-4 checkpoint must be self-describing.  Do not silently
        # downgrade a corrupt full snapshot to history-only recovery: that
        # would lose read-file guards, compaction state and provenance.
        if expected_revision and expected_revision != actual_revision:
            raise ValueError("Checkpoint context snapshot revision mismatch")
        if (
            int(checkpoint.schema_version or 0) >= 4
            and not expected_revision
        ):
            raise ValueError("Checkpoint context snapshot revision is missing")
        snapshot.setdefault(
            "context_schema_version", CONTEXT_SNAPSHOT_SCHEMA_VERSION
        )
        # Keep the in-memory projection canonical even when a legacy producer
        # stored the revision only at the envelope level.
        snapshot["context_revision"] = actual_revision
    else:
        # Schema <= 3 checkpoints only carried the history projection.
        snapshot = {"history": checkpoint.messages}
    context_builder.load_snapshot(snapshot)
    # Skill instructions are already present in the restored message history.
    # Do not reactivate them: Codex treats explicit Skill selection as
    # turn-scoped input, not persistent session state.
    _ = skill_manager

    resume_payload = checkpoint.resume_payload or {}
    for key, value in resume_payload.items():
        if key in _RESERVED_RESUME_KEYS or key.startswith("_query_engine_"):
            continue
        metadata[key] = value
    metadata["checkpoint_origin"] = {
        "run_id": checkpoint.run_id,
        "conversation_id": checkpoint.conversation_id,
        "session_id": checkpoint.session_id,
        "sequence": checkpoint.sequence,
        "timestamp": checkpoint.timestamp,
        "stopped_reason": checkpoint.stopped_reason or "",
        "schema_version": int(checkpoint.schema_version or 1),
        "context_snapshot_present": has_context_snapshot,
        "context_schema_version": (
            int(snapshot.get("context_schema_version") or 0)
            if has_context_snapshot
            else 0
        ),
        "context_revision": (
            str(
                checkpoint.context_revision
                or snapshot.get("context_revision")
                or ""
            )
            if has_context_snapshot
            else ""
        ),
    }

    # Assert the durable identity after importing the non-authoritative payload.
    metadata["run_id"] = current_run_id
    metadata["conversation_id"] = conversation_id
    metadata["_query_engine_recovery_restored"] = True

    notice = AgentEvent(
        type="system_notice",
        data={
            "title": "Resumed from checkpoint",
            "message": (
                f"Continuing from iteration {checkpoint.iterations}. "
                f"Previous stop reason: {checkpoint.stopped_reason}"
            ),
            "checkpoint_origin": dict(metadata["checkpoint_origin"]),
        },
    )
    return QueryRecoveryResult(checkpoint=checkpoint, startup_events=(notice,))
