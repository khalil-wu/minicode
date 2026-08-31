from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.agent.checkpoint import load_latest_checkpoint
from backend.agent.execution_journal import ExecutionJournal
from backend.agent.loop import AgentLoopSessionContext
from backend.agent.message import AgentEvent
from backend.agent.query_engine import AgentSession, QueryEngine, QuerySubmission
from backend.agent.runtime import AgentRuntime, TerminalCommitError
from backend.agent.run_context import RunContext
from backend.agent.turn_input import TurnInputQueue
from backend.agent.turn_kernel import TurnKernel
from backend.artifact.store import ArtifactStore
from backend.config import AgentSettings, PermissionSettings, TokenBudget
from backend.permissions.checker import PermissionChecker
from backend.tools.registry import ToolRegistry


def _runtime(tmp_path: Path) -> AgentRuntime:
    return AgentRuntime(
        metrics_file=tmp_path / "metrics.jsonl",
        swarm_store_dir=tmp_path / "swarm",
        enable_lease_heartbeat=False,
    )


def _submission(
    tmp_path: Path,
    *,
    runtime: AgentRuntime,
    runner,
    metadata: dict[str, object] | None = None,
    lifecycle_observer_factory=None,
    run_context: RunContext | None = None,
) -> QuerySubmission:
    return QuerySubmission(
        user_message="run the turn",
        session=AgentSession(
            llm=object(),
            tool_registry=ToolRegistry(),
            artifact_store=ArtifactStore(storage_dir=tmp_path / "artifacts"),
            permission_checker=PermissionChecker(PermissionSettings(), tmp_path),
            agent_settings=AgentSettings(max_iterations=1),
            token_budget=TokenBudget(),
            lifecycle_observer_factory=lifecycle_observer_factory,
        ),
        runtime=AgentLoopSessionContext(
            task_id="task-terminal-story",
            metadata=dict(metadata or {}),
            run_context=run_context or RunContext(agent_runtime=runtime),
        ),
    )


def _collect(stream) -> list[AgentEvent]:
    async def _go() -> list[AgentEvent]:
        return [event async for event in stream]

    return asyncio.run(_go())


def test_strict_terminal_commit_never_mutates_memory_after_cas_rejection(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    try:
        record = runtime.start_run(run_id="run-cas", conversation_id="conversation-cas")
        runtime._swarm_store.upsert_agent_run = lambda *args, **kwargs: None  # type: ignore[method-assign]

        with pytest.raises(TerminalCommitError, match="CAS rejected") as caught:
            runtime.commit_terminal(record.run_id, summary="should not publish")

        assert caught.value.failure_kind == "cas_rejected"
        assert runtime.get_run(record.run_id).status == "running"
        persisted = runtime._swarm_store.list_agent_runs(  # type: ignore[attr-defined]
            conversation_id="conversation-cas"
        )
        assert persisted[0]["status"] == "running"
    finally:
        runtime.close(release_lease=True)


def test_turn_kernel_exposes_terminal_commit_failure_without_completion(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    try:
        state = SimpleNamespace(
            conversation_id="conversation-kernel-cas",
            terminal_status=None,
            stopped_reason=None,
        )
        kernel = TurnKernel.create(
            metadata={"run_id": "run-kernel-cas"},
            state=state,
            budget=TokenBudget(),
            task_id="task-kernel-cas",
            session_id="session-kernel-cas",
            emit_event=None,
            initial_user_message="hello",
            run_context=RunContext(agent_runtime=runtime),
        )
        runtime._swarm_store.upsert_agent_run = lambda *args, **kwargs: None  # type: ignore[method-assign]

        event = kernel.complete_for_terminal_reason("completed")

        assert event is not None
        assert event.type == "error"
        assert event.data["terminal_commit_failed"] is True
        assert event.data["error_type"] == "terminal_commit_failed"
        assert kernel.completion_emitted is False
        assert runtime.get_run("run-kernel-cas").status == "running"
    finally:
        runtime.close(release_lease=True)


def test_runner_construction_failure_aborts_durable_running_record(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)

    def broken_runner(**_kwargs):
        raise RuntimeError("runner construction exploded")

    try:
        events = _collect(
            QueryEngine(runner=broken_runner).submit(
                _submission(tmp_path, runtime=runtime, runner=broken_runner)
            )
        )

        assert [event.type for event in events] == [
            "error",
            "agent.run.completed",
            "done",
        ]
        assert events[-1].data["status"] == "failed"
        assert events[-1].data["reason"] == "startup_failed"
        runs = runtime.list_runs(conversation_id="")["runs"]
        assert len(runs) == 1
        assert runs[0]["status"] == "failed"
        assert runs[0]["summary"] == "startup_failed"
    finally:
        runtime.close(release_lease=True)


def test_turn_kernel_construction_failure_publishes_startup_terminal(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)

    class BrokenTurnInputQueue(TurnInputQueue):
        def begin_turn(self, _run_id: str) -> None:
            raise RuntimeError("turn input owner failed")

    async def runner(**_kwargs) -> AsyncIterator[AgentEvent]:
        yield AgentEvent.done(status="completed")

    try:
        events = _collect(
            QueryEngine(runner=runner).submit(
                _submission(
                    tmp_path,
                        runtime=runtime,
                        runner=runner,
                        run_context=RunContext(
                            agent_runtime=runtime,
                            turn_input_queue=BrokenTurnInputQueue(),
                        ),
                )
            )
        )

        assert [event.type for event in events] == [
            "error",
            "agent.run.completed",
            "done",
        ]
        assert events[-1].data["reason"] == "startup_failed"
        assert runtime.list_runs()["runs"][0]["status"] == "failed"
    finally:
        runtime.close(release_lease=True)


def test_observer_finish_failure_cannot_block_canonical_terminal(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)

    async def runner(**_kwargs) -> AsyncIterator[AgentEvent]:
        yield AgentEvent.agent_message_completed("observer-safe final answer", source="model_final")
        yield AgentEvent.done(status="completed")

    class BrokenObserver:
        async def start(self) -> None:
            return None

        async def observe(self, event) -> None:
            del event

        async def finish(
            self, *, status: str = "completed", reason: str = ""
        ) -> None:
            del status, reason
            raise RuntimeError("observer finish exploded")

    def broken_observer_factory(**_kwargs):
        return BrokenObserver()
    try:
        events = _collect(
            QueryEngine(runner=runner).submit(
                _submission(
                    tmp_path,
                        runtime=runtime,
                        runner=runner,
                        lifecycle_observer_factory=broken_observer_factory,
                )
            )
        )

        assert any(event.type == "agent.run.completed" for event in events)
        assert any(event.type == "done" and event.data["status"] == "completed" for event in events)
        runs = runtime.list_runs()["runs"]
        assert runs[0]["status"] == "completed"
    finally:
        runtime.close(release_lease=True)


def test_query_engine_cas_failure_cannot_emit_success_terminal(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)

    async def runner(**kwargs) -> AsyncIterator[AgentEvent]:
        turn_kernel = kwargs["turn_kernel"]
        turn_kernel.runtime._swarm_store.upsert_agent_run = (  # type: ignore[method-assign]
            lambda *args, **inner_kwargs: None
        )
        yield AgentEvent.done(status="completed")

    try:
        events = _collect(
            QueryEngine(runner=runner).submit(
                _submission(tmp_path, runtime=runtime, runner=runner)
            )
        )

        errors = [event for event in events if event.type == "error"]
        assert errors and errors[-1].data["terminal_commit_failed"] is True
        done = next(event for event in events if event.type == "done")
        assert done.data["status"] == "failed"
        assert done.data["reason"] == "terminal_commit_failed"
        assert not any(event.type == "agent.run.completed" for event in events)
        assert runtime.list_runs()["runs"][0]["status"] == "running"
    finally:
        runtime.close(release_lease=True)


def test_terminal_journal_synthetic_closes_unresolved_tool_use(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    journal = ExecutionJournal(
        agent_id="journal-terminal",
        base_dir=tmp_path / "journals",
    )

    async def runner(**_kwargs) -> AsyncIterator[AgentEvent]:
        yield AgentEvent.tool_call("tool-1", "read_file", {"path": "README.md"})
        yield AgentEvent.done(status="cancelled", reason="user_interrupted")

    try:
        events = _collect(
            QueryEngine(runner=runner).submit(
                _submission(
                    tmp_path,
                        runtime=runtime,
                        runner=runner,
                        run_context=RunContext(
                            agent_runtime=runtime,
                            execution_journal=journal,
                        ),
                )
            )
        )

        assert any(event.type == "done" for event in events)
        assert journal.unresolved_tool_uses() == []
        terminal = [event for event in journal.read_events() if event.event_type == "terminal"]
        assert terminal
        assert terminal[-1].payload["unresolved_tool_uses"] == []
        synthetic = [
            event
            for event in journal.read_events()
            if event.event_type == "tool_result" and event.payload.get("synthetic")
        ]
        assert synthetic
        assert synthetic[-1].payload["status"] == "cancelled"
    finally:
        runtime.close(release_lease=True)


def test_terminal_journal_recovery_requires_runtime_commit_receipt(tmp_path: Path) -> None:
    journal = ExecutionJournal(
        agent_id="journal-receipt-gate",
        base_dir=tmp_path / "journals",
    )
    intent = journal.append_lifecycle(
        "terminal_intent",
        {
            "run_id": "run-receipt-gate",
            "conversation_id": "conversation-receipt-gate",
            "message_id": "message-receipt-gate",
            "assistant_message": {
                "id": "message-receipt-gate",
                "role": "assistant",
                "content": "committed answer",
                "terminal_status": "completed",
                "termination_reason": "",
            },
            "context_snapshot": {"history": [{"role": "assistant", "content": "committed answer"}]},
        },
    )

    assert journal.unprojected_terminal_projections() == []

    journal.append_lifecycle(
        "runtime_terminal_committed",
        {
            "run_id": "run-receipt-gate",
            "status": "completed",
            "terminal_intent_event_id": intent.event_id,
        },
    )

    projections = journal.unprojected_terminal_projections()
    assert len(projections) == 1
    assert projections[0]["source_event_id"] == intent.event_id
    assert projections[0]["assistant_message"]["content"] == "committed answer"


def test_query_engine_cas_rejection_never_publishes_recoverable_terminal(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    journal = ExecutionJournal(
        agent_id="journal-cas-rejection",
        base_dir=tmp_path / "journals",
    )

    async def runner(**kwargs) -> AsyncIterator[AgentEvent]:
        kwargs["turn_kernel"].runtime._swarm_store.upsert_agent_run = (  # type: ignore[method-assign]
            lambda *args, **inner_kwargs: None
        )
        yield AgentEvent(
            type="agent.terminal.intent",
            data={"status": "completed", "reason": ""},
        )
        yield AgentEvent.done(status="completed")

    try:
        events = _collect(
            QueryEngine(runner=runner).submit(
                _submission(
                    tmp_path,
                        runtime=runtime,
                        runner=runner,
                        run_context=RunContext(
                            agent_runtime=runtime,
                            execution_journal=journal,
                        ),
                        metadata={
                            "assistant_message_id": "message-cas-rejection",
                        },
                )
            )
        )

        assert any(
            event.type == "error" and event.data.get("terminal_commit_failed") is True
            for event in events
        )
        assert not any(event.event_type == "assistant" for event in journal.read_events())
        assert not any(event.event_type == "terminal" for event in journal.read_events())
        lifecycles = {
            str(event.payload.get("lifecycle") or "")
            for event in journal.read_events()
            if event.event_type == "system"
        }
        assert "terminal_intent" in lifecycles
        assert "runtime_terminal_commit_failed" in lifecycles
        assert "runtime_terminal_committed" not in lifecycles
        assert journal.unprojected_terminal_projections() == []
    finally:
        runtime.close(release_lease=True)


def test_query_engine_cas_failure_keeps_resumable_checkpoint(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MINICODE_STATE_ROOT", str(tmp_path / "state"))
    runtime = _runtime(tmp_path)
    session_id = "session-checkpoint-cas"
    conversation_id = "conversation-checkpoint-cas"

    async def runner(**kwargs) -> AsyncIterator[AgentEvent]:
        state = kwargs["state"]
        state.conversation_id = conversation_id
        kwargs["context_builder"].append_user("durable answer boundary")
        # A mid-run checkpoint is the resume handle. A completed turn never
        # writes one; the clear of the existing handle is deferred until the
        # terminal commit lands.
        state.stopped_reason = "timeout"
        state.terminal_status = "partial"
        assert kwargs["turn_kernel"].finalize_checkpoint(
            session_id=session_id,
            user_message=kwargs["user_message"],
            state=state,
            context_builder=kwargs["context_builder"],
            defer_completed_clear=True,
        ) == "saved"
        state.stopped_reason = "completed"
        state.terminal_status = "completed"
        assert kwargs["turn_kernel"].finalize_checkpoint(
            session_id=session_id,
            user_message=kwargs["user_message"],
            state=state,
            context_builder=kwargs["context_builder"],
            defer_completed_clear=True,
        ) == "pending_clear"
        runtime._swarm_store.upsert_agent_run = (  # type: ignore[method-assign]
            lambda *args, **inner_kwargs: None
        )
        yield AgentEvent.done(status="completed")

    submission = _submission(
        tmp_path,
        runtime=runtime,
        runner=runner,
        metadata={"conversation_id": conversation_id},
    )
    submission.runtime.session_id = session_id

    try:
        events = _collect(QueryEngine(runner=runner).submit(submission))
        assert any(
            event.type == "error" and event.data.get("terminal_commit_failed") is True
            for event in events
        )
        checkpoint = load_latest_checkpoint(
            session_id,
            conversation_id=conversation_id,
        )
        # The commit failed, so the deferred clear must not have run.
        assert checkpoint is not None
        assert checkpoint.stopped_reason == "timeout"
    finally:
        runtime.close(release_lease=True)
