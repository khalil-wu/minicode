from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.agent.checkpoint import (
    CHECKPOINT_SCHEMA_VERSION,
    AgentCheckpoint,
    CheckpointCorruptionError,
    _compute_checksum,
    context_snapshot_revision,
    load_latest_checkpoint,
    save_checkpoint,
)
from backend.agent.context import CompactionNoopError, ContextBuilder
from backend.agent.execution_journal import ExecutionJournal
from backend.agent.loop import AgentLoopSessionContext
from backend.agent.message import AgentEvent
from backend.agent.query_engine import AgentSession, QueryEngine, QuerySubmission
from backend.agent.query_recovery import prepare_query_recovery
from backend.agent.runtime import AgentRuntime
from backend.agent.run_context import RunContext
from backend.agent.state import AgentState
from backend.agent.turn_kernel import TurnKernel
from backend.artifact.store import ArtifactStore
from backend.config import AgentSettings, PermissionSettings, TokenBudget
from backend.permissions.checker import PermissionChecker
from backend.tools.registry import ToolRegistry
from backend.ws.agent_runner import _commit_automatic_compaction


def _snapshot() -> dict:
    return {
        "history": [{"role": "user", "content": "resume this work"}],
        "history_frozen_count": 1,
        "persistent_notes": [
            {
                "kind": "task",
                "title": "durable fact",
                "content": "keep the checkpoint metadata",
            }
        ],
        "read_file_hashes": {"C:/workspace/app.py": "sha256-app"},
        "compaction_count": 2,
        "git_status_context": "M backend/agent/checkpoint.py",
        "git_status_workspace": "C:/workspace",
        "invoked_skills": [],
        "context_ledger": {
            "schema_version": 1,
            "estimated_tokens": 42,
            "actual_tokens": 40,
            "compaction_count": 2,
            "native_attachment_tokens": 0,
            "native_attachment_count": 0,
            "entries": [],
        },
    }


def test_latest_completed_checkpoint_blocks_older_incomplete_resume(tmp_path) -> None:
    common = {
        "session_id": "completed-barrier",
        "user_message": "work",
        "iterations": 1,
        "reply": "done",
        "messages": [{"role": "user", "content": "work"}],
        "tool_calls": [],
        "active_skills": [],
        "disabled_tools": set(),
        "last_mutation_index": 0,
        "base_dir": tmp_path,
        "conversation_id": "conversation-1",
    }
    save_checkpoint(stopped_reason="timeout", **common)
    save_checkpoint(stopped_reason="completed", **common)

    assert load_latest_checkpoint(
        "completed-barrier",
        base_dir=tmp_path,
        conversation_id="conversation-1",
    ) is None


def _save_full_snapshot(tmp_path: Path, *, session_id: str = "snapshot-session") -> Path:
    snapshot = _snapshot()
    return save_checkpoint(
        session_id=session_id,
        user_message="continue",
        iterations=3,
        reply="partial",
        messages=list(snapshot["history"]),
        context_snapshot=snapshot,
        tool_calls=[],
        active_skills=[],
        disabled_tools=set(),
        stopped_reason="timeout",
        last_mutation_index=0,
        base_dir=tmp_path,
        run_id="run-snapshot",
        conversation_id="conversation-snapshot",
    )


def test_schema4_checkpoint_persists_and_restores_complete_context_snapshot(
    tmp_path: Path,
) -> None:
    path = _save_full_snapshot(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["schema_version"] == CHECKPOINT_SCHEMA_VERSION == 4
    assert payload["context_snapshot"]["context_schema_version"] == 1
    assert payload["context_revision"] == payload["context_snapshot"]["context_revision"]
    assert payload["context_revision"] == context_snapshot_revision(
        payload["context_snapshot"]
    )
    assert payload["checksum"] == _compute_checksum(payload)

    checkpoint = load_latest_checkpoint(
        "snapshot-session",
        base_dir=tmp_path,
        conversation_id="conversation-snapshot",
    )
    assert checkpoint is not None
    assert checkpoint.context_revision == payload["context_revision"]

    restored = ContextBuilder()
    restored.load_snapshot(checkpoint.context_snapshot)
    round_trip = restored.export_snapshot()

    assert round_trip["history"][0]["content"] == "resume this work"
    assert round_trip["history_frozen_count"] == 1
    assert round_trip["persistent_notes"] == _snapshot()["persistent_notes"]
    assert list(round_trip["read_file_hashes"].values()) == ["sha256-app"]
    assert round_trip["compaction_count"] == 2
    assert round_trip["git_status_context"] == "M backend/agent/checkpoint.py"
    assert round_trip["git_status_workspace"] == "C:/workspace"


def test_context_revision_rejects_tampering_even_with_recomputed_envelope_checksum(
    tmp_path: Path,
) -> None:
    path = _save_full_snapshot(tmp_path, session_id="tamper-session")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["context_snapshot"]["persistent_notes"][0]["content"] = "tampered"
    # Prove that context revision is an independent fence rather than merely
    # an alias for the outer checkpoint checksum.
    payload["checksum"] = _compute_checksum(payload)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )

    with pytest.raises(CheckpointCorruptionError, match="verification failed"):
        load_latest_checkpoint(
            "tamper-session",
            base_dir=tmp_path,
            conversation_id="conversation-snapshot",
        )


def test_legacy_history_only_checkpoint_remains_resumable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkpoint = AgentCheckpoint(
        session_id="legacy-session",
        timestamp=12.0,
        user_message="legacy request",
        iterations=4,
        reply="legacy partial",
        messages=[{"role": "assistant", "content": "legacy evidence"}],
        tool_calls=[],
        active_skills=[],
        disabled_tools=[],
        stopped_reason="timeout",
        run_id="legacy-run",
        conversation_id="legacy-conversation",
        schema_version=3,
        sequence=8,
    )
    monkeypatch.setattr(
        "backend.agent.query_recovery.load_latest_checkpoint",
        lambda *_args, **_kwargs: checkpoint,
    )
    state = AgentState(user_message="resume", max_iterations=2)
    context = ContextBuilder()
    metadata = {"resume_from_checkpoint": True}

    result = prepare_query_recovery(
        session_id="legacy-session",
        conversation_id="legacy-conversation",
        metadata=metadata,
        state=state,
        context_builder=context,
        max_iterations_budget=2,
        current_run_id="new-run",
    )

    assert result.restored is True
    assert context.export_snapshot()["history"][0]["content"] == "legacy evidence"
    assert metadata["run_id"] == "new-run"
    assert metadata["checkpoint_origin"]["schema_version"] == 3
    assert metadata["checkpoint_origin"]["context_snapshot_present"] is False
    assert metadata["checkpoint_origin"]["context_revision"] == ""


def test_full_snapshot_recovery_preserves_revision_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _save_full_snapshot(tmp_path, session_id="full-recovery-session")
    checkpoint = load_latest_checkpoint(
        "full-recovery-session",
        base_dir=tmp_path,
        conversation_id="conversation-snapshot",
    )
    assert checkpoint is not None
    monkeypatch.setattr(
        "backend.agent.query_recovery.load_latest_checkpoint",
        lambda *_args, **_kwargs: checkpoint,
    )
    state = AgentState(user_message="resume", max_iterations=2)
    context = ContextBuilder()
    metadata = {"resume_from_checkpoint": True}

    result = prepare_query_recovery(
        session_id="full-recovery-session",
        conversation_id="conversation-snapshot",
        metadata=metadata,
        state=state,
        context_builder=context,
        max_iterations_budget=2,
        current_run_id="new-full-run",
    )

    restored = context.export_snapshot()
    assert result.restored is True
    assert restored["persistent_notes"] == _snapshot()["persistent_notes"]
    assert restored["compaction_count"] == 2
    assert metadata["checkpoint_origin"]["context_snapshot_present"] is True
    assert metadata["checkpoint_origin"]["context_schema_version"] == 1
    assert (
        metadata["checkpoint_origin"]["context_revision"]
        == checkpoint.context_revision
    )
    assert metadata["run_id"] == "new-full-run"


def test_turn_kernel_records_checkpoint_save_and_clear_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MINICODE_STATE_ROOT", str(tmp_path / "state"))
    runtime = AgentRuntime(
        metrics_file=tmp_path / "metrics.jsonl",
        swarm_store_dir=tmp_path / "swarm",
        enable_lease_heartbeat=False,
    )
    state = AgentState(user_message="work")
    state.conversation_id = "checkpoint-conversation"
    state.stopped_reason = "timeout"
    context = ContextBuilder()
    context.load_snapshot(_snapshot())
    kernel = TurnKernel.create(
        metadata={},
        state=state,
        budget=TokenBudget(),
        task_id="checkpoint-task",
        session_id="checkpoint-session",
        emit_event=None,
        initial_user_message="work",
        run_context=RunContext(agent_runtime=runtime),
    )
    try:
        kernel.metadata["checkpoint_origin"] = {
            "run_id": "parent-run",
            "sequence": 7,
            "context_revision": "parent-revision",
        }
        assert kernel.finalize_checkpoint(
            session_id="checkpoint-session",
            user_message="work",
            state=state,
            context_builder=context,
        ) == "saved"
        saved_evidence = kernel.checkpoint_evidence()
        assert saved_evidence["status"] == "saved"
        assert saved_evidence["schema_version"] == 4
        assert saved_evidence["sequence"] >= 1
        assert len(saved_evidence["context_revision"]) == 64
        checkpoint = load_latest_checkpoint(
            "checkpoint-session",
            conversation_id="checkpoint-conversation",
        )
        assert checkpoint is not None
        assert checkpoint.resume_payload["parent_checkpoint"] == {
            "run_id": "parent-run",
            "sequence": 7,
            "context_revision": "parent-revision",
        }

        monkeypatch.setattr(
            "backend.agent.turn_kernel.save_run_checkpoint",
            lambda **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
        )
        assert kernel.finalize_checkpoint(
            session_id="checkpoint-session",
            user_message="work",
            state=state,
            context_builder=context,
        ) == "save_failed"
        assert kernel.checkpoint_evidence() == {
            "status": "save_failed",
            "error_type": "OSError",
        }

        state.stopped_reason = "completed"
        state.terminal_status = "completed"
        monkeypatch.setattr(
            "backend.agent.turn_kernel.clear_checkpoints",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                PermissionError("checkpoint locked")
            ),
        )
        assert kernel.finalize_checkpoint(
            session_id="checkpoint-session",
            user_message="work",
            state=state,
            context_builder=context,
        ) == "clear_failed"
        assert kernel.checkpoint_evidence()["status"] == "clear_failed"
        assert kernel.checkpoint_evidence()["error_type"] == "PermissionError"
    finally:
        runtime.close(release_lease=True)


def test_query_terminal_and_journal_share_checkpoint_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MINICODE_STATE_ROOT", str(tmp_path / "state"))
    runtime = AgentRuntime(
        metrics_file=tmp_path / "metrics.jsonl",
        swarm_store_dir=tmp_path / "swarm",
        enable_lease_heartbeat=False,
    )
    journal = ExecutionJournal("checkpoint-journal", base_dir=tmp_path / "journals")

    async def runner(**kwargs):
        state = kwargs["state"]
        state.stopped_reason = "timeout"
        state.terminal_status = "partial"
        kwargs["context_builder"].load_snapshot(_snapshot())
        assert kwargs["turn_kernel"].finalize_checkpoint(
            session_id=kwargs["session_id"],
            user_message=kwargs["user_message"],
            state=state,
            context_builder=kwargs["context_builder"],
        ) == "saved"
        yield AgentEvent.done(status="partial", reason="timeout")

    state = AgentState(user_message="continue", max_iterations=1)
    state.conversation_id = "query-checkpoint-conversation"
    submission = QuerySubmission(
        user_message="continue",
        session=AgentSession(
            llm=object(),
            tool_registry=ToolRegistry(),
            artifact_store=ArtifactStore(storage_dir=tmp_path / "artifacts"),
            permission_checker=PermissionChecker(PermissionSettings(), tmp_path),
            agent_settings=AgentSettings(max_iterations=1),
            token_budget=TokenBudget(),
            context_builder=ContextBuilder(),
        ),
        state=state,
        runtime=AgentLoopSessionContext(
            session_id="query-checkpoint-session",
            run_context=RunContext(
                agent_runtime=runtime,
                execution_journal=journal,
            ),
            metadata={
                "conversation_id": "query-checkpoint-conversation",
            },
        ),
    )

    async def collect() -> list[AgentEvent]:
        return [event async for event in QueryEngine(runner=runner).submit(submission)]

    try:
        events = asyncio.run(collect())
        done = next(event for event in events if event.type == "done")
        terminal = next(
            event for event in journal.read_events() if event.event_type == "terminal"
        )

        assert done.data["checkpoint"]["status"] == "saved"
        assert done.data["checkpoint"]["context_revision"]
        assert terminal.payload["checkpoint"] == done.data["checkpoint"]
    finally:
        runtime.close(release_lease=True)


def test_noop_compaction_does_not_advance_context_revision() -> None:
    context = ContextBuilder()
    context.append_user("too short to compact")
    before = context.export_snapshot()

    with pytest.raises(CompactionNoopError, match="Nothing to compact"):
        asyncio.run(context.compact())
    after = context.export_snapshot()

    assert after["compaction_count"] == before["compaction_count"] == 0
    assert context_snapshot_revision(after) == context_snapshot_revision(before)


def test_automatic_compaction_commits_replacement_snapshot_before_continuing() -> None:
    committed: dict = {}
    builder = SimpleNamespace(
        export_snapshot=lambda: {
            "history": [
                {
                    "role": "user",
                    "content": "The conversation history before this point was compacted",
                }
            ],
            "compaction_count": 1,
        }
    )

    class _Repository:
        def get_conversation(self, conversation_id: str):
            assert conversation_id == "conversation-auto-compact"
            return SimpleNamespace(
                revision=7,
                context_snapshot={"ui_agent_state": {"plan": {"plan": []}}},
            )

        def commit_compaction(self, conversation_id: str, **kwargs):
            committed.update({"conversation_id": conversation_id, **kwargs})
            return SimpleNamespace(id=conversation_id)

    saved = asyncio.run(
        _commit_automatic_compaction(
            _Repository(),
            conversation_id="conversation-auto-compact",
            context_builder=builder,
            summary="durable compacted summary",
        )
    )

    assert committed["expected_revision"] == 7
    assert committed["state"] == "compacted"
    assert committed["summary"] == "durable compacted summary"
    assert committed["context_snapshot"] == saved
    assert saved["compaction_count"] == 1
    assert saved["ui_agent_state"] == {"plan": {"plan": []}}


def test_automatic_compaction_fails_closed_without_canonical_commit() -> None:
    repository = SimpleNamespace(
        get_conversation=lambda _conversation_id: SimpleNamespace(
            revision=1,
            context_snapshot={},
        )
    )
    builder = SimpleNamespace(export_snapshot=lambda: {"history": []})

    with pytest.raises(RuntimeError, match="canonical compaction commit"):
        asyncio.run(
            _commit_automatic_compaction(
                repository,
                conversation_id="conversation-auto-compact",
                context_builder=builder,
                summary="summary",
            )
        )
