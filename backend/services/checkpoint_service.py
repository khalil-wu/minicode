from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class CheckpointServiceError(ValueError):
    """User-recoverable checkpoint operation failure."""


@dataclass
class CheckpointListResult:
    conversation_id: str
    checkpoints: list[dict[str, Any]]


@dataclass
class CheckpointRewindResult:
    conversation_id: str
    checkpoint: dict[str, Any]


@dataclass
class RunCheckpointListResult:
    session_id: str
    conversation_id: str
    checkpoints: list[dict[str, Any]]
    runtime_snapshot: dict[str, Any]


@dataclass
class RunCheckpointResumeResult:
    session_id: str
    conversation_id: str
    run_id: str
    iteration: int
    stopped_reason: str | None
    user_message: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "type": "checkpoint.run.resume",
            "resumed": True,
            "session_id": self.session_id,
            "conversation_id": self.conversation_id,
            "checkpoint_run_id": self.run_id,
            "iteration": self.iteration,
            "stopped_reason": self.stopped_reason,
        }


def list_checkpoints(
    checkpoint_manager: Any,
    *,
    conversation_id: str,
    session_id: str = "",
    workspace_root: str = "",
    limit: int = 50,
) -> CheckpointListResult:
    target_conversation_id = str(conversation_id or "").strip()
    if not target_conversation_id:
        raise CheckpointServiceError("No active conversation for checkpoint.list")
    bounded_limit = max(1, min(int(limit or 50), 200))
    expected_session = str(session_id or "").strip()
    expected_workspace = _resolved_workspace(workspace_root)
    records = checkpoint_manager.list_for_conversation(target_conversation_id, limit=200)
    records = [
        record
        for record in records
        if (not expected_session or str(record.session_id or "").strip() == expected_session)
        and (
            expected_workspace is None
            or _resolved_workspace(record.workspace_root) == expected_workspace
        )
    ][:bounded_limit]
    return CheckpointListResult(
        conversation_id=target_conversation_id,
        checkpoints=[record.to_public_dict() for record in records],
    )


async def rewind_checkpoint(
    checkpoint_manager: Any,
    checkpoint_id: str,
    *,
    conversation_id: str = "",
    session_id: str = "",
    workspace_root: str = "",
) -> CheckpointRewindResult:
    clean_checkpoint_id = str(checkpoint_id or "").strip()
    if not clean_checkpoint_id:
        raise CheckpointServiceError("checkpoint_id is required")
    try:
        record = checkpoint_manager.get(clean_checkpoint_id)
        if record is None:
            raise CheckpointServiceError("Checkpoint was not found.")
        expected_conversation = str(conversation_id or "").strip()
        expected_session = str(session_id or "").strip()
        if expected_conversation and str(record.conversation_id or "").strip() != expected_conversation:
            raise CheckpointServiceError("Checkpoint does not belong to the active conversation.")
        if expected_session and str(record.session_id or "").strip() != expected_session:
            raise CheckpointServiceError("Checkpoint does not belong to the active session.")
        expected_workspace = _resolved_workspace(workspace_root)
        if expected_workspace is not None and _resolved_workspace(record.workspace_root) != expected_workspace:
            raise CheckpointServiceError("Checkpoint does not belong to the active workspace.")
        record = await checkpoint_manager.rewind(clean_checkpoint_id)
    except Exception as exc:
        raise CheckpointServiceError(f"Checkpoint rewind failed: {exc}") from exc
    return CheckpointRewindResult(
        conversation_id=record.conversation_id,
        checkpoint=record.to_public_dict(),
    )


def _resolved_workspace(value: str) -> Path | None:
    clean = str(value or "").strip()
    if not clean:
        return None
    return Path(clean).expanduser().resolve()


def list_run_checkpoints(
    *,
    session_id: str,
    conversation_id: str = "",
    runtime: Any | None = None,
) -> RunCheckpointListResult:
    from backend.agent.checkpoint import get_checkpoint_dir, validate_storage_id
    from backend.agent.runtime import default_runtime

    clean_session_id = str(session_id or "").strip()
    clean_conversation_id = str(conversation_id or "").strip()
    checkpoints: list[dict[str, Any]] = []
    if clean_session_id:
        try:
            clean_session_id = validate_storage_id(clean_session_id, field_name="session_id")
        except ValueError as exc:
            raise CheckpointServiceError(str(exc)) from exc
        checkpoint_dir = get_checkpoint_dir(clean_session_id)
        for path in sorted(checkpoint_dir.glob("*.json"), reverse=True)[:50]:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError) as exc:
                raise CheckpointServiceError(
                    f"Checkpoint file '{path.name}' is unreadable: {exc}"
                ) from exc
            if not isinstance(payload, dict):
                raise CheckpointServiceError(
                    f"Checkpoint file '{path.name}' is invalid: expected an object"
                )
            if clean_conversation_id and str(payload.get("conversation_id") or "").strip() != clean_conversation_id:
                continue
            iterations = int(payload.get("iterations") or 0)
            checkpoints.append(
                {
                    "run_id": str(payload.get("run_id") or ""),
                    "session_id": str(payload.get("session_id") or clean_session_id),
                    "conversation_id": str(payload.get("conversation_id") or ""),
                    "iteration": iterations,
                    "iterations": iterations,
                    "stopped_reason": payload.get("stopped_reason"),
                    "timestamp": payload.get("timestamp"),
                    "created_at": payload.get("timestamp"),
                }
            )
    runtime_obj = runtime or default_runtime()
    runtime_snapshot = runtime_obj.list_runs(conversation_id=clean_conversation_id, include_subagents=True)
    return RunCheckpointListResult(
        session_id=clean_session_id,
        conversation_id=clean_conversation_id,
        checkpoints=checkpoints,
        runtime_snapshot=dict(runtime_snapshot),
    )


def prepare_run_checkpoint_resume(
    *,
    session_id: str,
    requested_conversation_id: str = "",
    active_conversation_id: str = "",
) -> RunCheckpointResumeResult | None:
    from backend.agent.checkpoint import (
        CheckpointError,
        load_latest_run_checkpoint,
        validate_storage_id,
    )

    clean_session_id = str(session_id or "").strip()
    if not clean_session_id:
        raise CheckpointServiceError("No active session ID. Cannot resume.")
    try:
        clean_session_id = validate_storage_id(clean_session_id, field_name="session_id")
    except ValueError as exc:
        raise CheckpointServiceError(str(exc)) from exc

    conversation_id = str(
        requested_conversation_id or active_conversation_id or ""
    ).strip()
    if not conversation_id:
        raise CheckpointServiceError("No active conversation. Cannot resume.")

    # A corrupt checkpoint is a user-visible resume failure, not an unhandled
    # error: CheckpointError is a RuntimeError, so without this the exception
    # escapes agent.resume entirely and the durable command path re-claims it
    # forever while the client never hears back.
    try:
        checkpoint = load_latest_run_checkpoint(
            clean_session_id,
            conversation_id=conversation_id,
        )
    except CheckpointError as exc:
        raise CheckpointServiceError(f"Cannot resume: {exc}") from exc
    if checkpoint is None:
        return None

    return RunCheckpointResumeResult(
        session_id=clean_session_id,
        conversation_id=conversation_id,
        run_id=checkpoint.run_id,
        iteration=checkpoint.iterations,
        stopped_reason=checkpoint.stopped_reason,
        user_message=checkpoint.user_message,
    )
