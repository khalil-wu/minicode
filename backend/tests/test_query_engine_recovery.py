from __future__ import annotations

import asyncio

from backend.agent.checkpoint import AgentCheckpoint
from backend.agent.context import ContextBuilder
from backend.agent.execution_journal import ExecutionJournal
from backend.agent.loop import AgentLoopSessionContext
from backend.agent.message import AgentEvent
from backend.agent.query_engine import AgentSession, QueryEngine, QuerySubmission
from backend.agent.runtime import AgentRuntime
from backend.agent.run_context import RunContext
from backend.agent.state import AgentState
from backend.artifact.store import ArtifactStore
from backend.config import AgentSettings, PermissionSettings, TokenBudget
from backend.permissions.checker import PermissionChecker
from backend.tools.registry import ToolRegistry


def test_query_engine_owns_checkpoint_restore_and_preserves_new_run_fence(
    tmp_path,
    monkeypatch,
) -> None:
    checkpoint = AgentCheckpoint(
        session_id="session-resume",
        timestamp=123.0,
        user_message="old request",
        iterations=7,
        reply="partial reply",
        messages=[{"role": "assistant", "content": "restored evidence"}],
        tool_calls=[
            {
                "tool_name": "read_file",
                "tool_input": {"file_path": "README.md"},
                "tool_output": "evidence",
            }
        ],
        active_skills=["repo-inspection"],
        disabled_tools=["write_file"],
        loaded_deferred_tools=["preview_server"],
        stopped_reason="timeout",
        last_mutation_index=4,
        run_id="stale-run-id",
        conversation_id="conversation-resume",
        resume_payload={
            "run_id": "payload-stale-run",
            "conversation_id": "payload-stale-conversation",
            "custom_resume_fact": "kept",
        },
        schema_version=3,
        sequence=9,
    )
    monkeypatch.setattr(
        "backend.agent.query_recovery.load_latest_checkpoint",
        lambda session_id, **kwargs: checkpoint,
    )
    captured: dict[str, object] = {}

    async def runner(**kwargs):
        captured.update(kwargs)
        yield AgentEvent.done(status="completed")

    state = AgentState(user_message="continue", max_iterations=5)
    runtime = AgentRuntime(metrics_file=tmp_path / "runtime.jsonl")
    context = AgentLoopSessionContext(
        session_id="session-resume",
        metadata={
            "resume_from_checkpoint": True,
            "conversation_id": "conversation-resume",
        },
        run_context=RunContext(agent_runtime=runtime),
    )
    submission = QuerySubmission(
        user_message="continue",
        session=AgentSession(
            llm=object(),
            tool_registry=ToolRegistry(),
            artifact_store=ArtifactStore(storage_dir=tmp_path / "artifacts"),
            permission_checker=PermissionChecker(PermissionSettings(), tmp_path),
            agent_settings=AgentSettings(max_iterations=5),
            token_budget=TokenBudget(),
            context_builder=ContextBuilder(),
        ),
        state=state,
        runtime=context,
    )

    async def collect():
        return [event async for event in QueryEngine(runner=runner).submit(submission)]

    events = asyncio.run(collect())

    restored_metadata = captured["metadata"]
    restored_context = captured["context_builder"]
    assert isinstance(restored_metadata, dict)
    assert restored_metadata["run_id"] not in {"stale-run-id", "payload-stale-run"}
    assert restored_metadata["conversation_id"] == "conversation-resume"
    assert restored_metadata["custom_resume_fact"] == "kept"
    assert restored_metadata["checkpoint_origin"]["run_id"] == "stale-run-id"
    assert restored_metadata["_query_engine_recovery_restored"] is True
    assert state.iterations == 7
    assert state.max_iterations >= 12
    assert state.reply == "partial reply"
    assert state.disabled_tools == {"write_file"}
    assert state.loaded_deferred_tools == {"preview_server"}
    assert state._last_mutation_index == 4
    assert len(state.tool_calls) == 1
    assert isinstance(restored_context, ContextBuilder)
    assert restored_context.export_snapshot()["history"][0]["content"] == "restored evidence"
    assert captured["turn_kernel"]._next_user_message == ""
    notices = [event for event in events if event.type == "system_notice"]
    assert len(notices) == 1
    assert notices[0].data["checkpoint_origin"]["sequence"] == 9


def test_query_engine_does_not_reclassify_runtime_bug_as_provider_error(tmp_path) -> None:
    async def broken_runner(**kwargs):
        del kwargs
        raise RuntimeError("internal projection detail must not leak")
        yield  # pragma: no cover - keeps this an async generator

    runtime = AgentRuntime(
        metrics_file=tmp_path / "runtime.jsonl",
        swarm_store_dir=tmp_path / "swarm",
    )
    submission = QuerySubmission(
        user_message="trigger runtime boundary",
        session=AgentSession(
            llm=object(),
            tool_registry=ToolRegistry(),
            artifact_store=ArtifactStore(storage_dir=tmp_path / "artifacts"),
            permission_checker=PermissionChecker(PermissionSettings(), tmp_path),
            agent_settings=AgentSettings(max_iterations=2),
            token_budget=TokenBudget(),
            context_builder=ContextBuilder(),
        ),
        state=AgentState(user_message="trigger runtime boundary", max_iterations=2),
        runtime=AgentLoopSessionContext(
            session_id="session-runtime-failure",
            run_context=RunContext(agent_runtime=runtime),
        ),
    )

    async def collect():
        return [
            event
            async for event in QueryEngine(runner=broken_runner).submit(submission)
        ]

    events = asyncio.run(collect())
    error = next(event for event in events if event.type == "error")
    done = next(event for event in events if event.type == "done")

    assert error.data["error_type"] == "runtime"
    assert error.data["recoverable"] is False
    assert "internal projection detail" not in error.data["message"]
    assert done.data["status"] == "failed"
    assert done.data["reason"] == "runtime_error"


def test_query_engine_journals_tool_claim_before_terminal_commit(tmp_path) -> None:
    journal = ExecutionJournal("main-journal", base_dir=tmp_path / "sidechains")

    async def runner(**_kwargs):
        yield AgentEvent.tool_call(
            "call-write",
            "write_file",
            {"file_path": "report.txt", "content": "done"},
            side_effect_kind="workspace",
            idempotent=False,
        )
        yield AgentEvent.done(status="cancelled", reason="user_interrupted")

    submission = QuerySubmission(
        user_message="write the report",
        session=AgentSession(
            llm=object(),
            tool_registry=ToolRegistry(),
            artifact_store=ArtifactStore(storage_dir=tmp_path / "artifacts"),
            permission_checker=PermissionChecker(PermissionSettings(), tmp_path),
            agent_settings=AgentSettings(max_iterations=2),
            token_budget=TokenBudget(),
            context_builder=ContextBuilder(),
        ),
        state=AgentState(user_message="write the report", max_iterations=2),
        runtime=AgentLoopSessionContext(
            session_id="session-journal",
            metadata={
                "conversation_id": "conversation-journal",
            },
            run_context=RunContext(execution_journal=journal),
        ),
    )

    async def collect():
        return [event async for event in QueryEngine(runner=runner).submit(submission)]

    asyncio.run(collect())
    events = journal.read_events()
    lifecycles = [
        event.payload.get("lifecycle")
        for event in events
        if event.event_type == "system"
    ]
    terminal = next(event for event in events if event.event_type == "terminal")

    assert lifecycles[:2] == ["turn_started", "provider_claimed"]
    assert lifecycles.index("terminal_intent") < lifecycles.index(
        "runtime_terminal_committed"
    )
    assert lifecycles.index("runtime_terminal_committed") < lifecycles.index(
        "provider_completed"
    )
    assert journal.unresolved_tool_uses() == []
    synthetic = next(
        event
        for event in journal.read_events()
        if event.event_type == "tool_result" and event.payload.get("synthetic")
    )
    assert synthetic.payload["tool_name"] == "write_file"
    assert synthetic.payload["status"] == "cancelled"
    assert terminal.payload["manual_recovery_required"] is False
    assert terminal.seq > next(
        event.seq for event in events if event.event_type == "tool_use"
    )


def test_query_engine_canonically_rejects_completed_tool_only_turn(tmp_path) -> None:
    async def runner(**_kwargs):
        yield AgentEvent.tool_call(
            "call-read",
            "read_file",
            {"file_path": "README.md"},
        )
        yield AgentEvent.tool_result(
            "call-read",
            "README content",
            status="success",
        )
        yield AgentEvent.done(status="completed")

    runtime = AgentRuntime(metrics_file=tmp_path / "runtime.jsonl")
    submission = QuerySubmission(
        user_message="read it and answer",
        session=AgentSession(
            llm=object(),
            tool_registry=ToolRegistry(),
            artifact_store=ArtifactStore(storage_dir=tmp_path / "artifacts"),
            permission_checker=PermissionChecker(PermissionSettings(), tmp_path),
            agent_settings=AgentSettings(max_iterations=2),
            token_budget=TokenBudget(),
            context_builder=ContextBuilder(),
        ),
        state=AgentState(user_message="read it and answer", max_iterations=2),
        runtime=AgentLoopSessionContext(
            session_id="session-tool-only",
            run_context=RunContext(agent_runtime=runtime),
        ),
    )

    async def collect():
        return [event async for event in QueryEngine(runner=runner).submit(submission)]

    events = asyncio.run(collect())
    completion = next(event for event in events if event.type == "agent.run.completed")
    terminal = next(event for event in events if event.type == "done")
    error = next(
        event
        for event in events
        if event.type == "error" and event.data.get("error_type") == "missing_final_answer"
    )

    assert completion.data["status"] == "partial"
    assert completion.data["summary"] == "missing_final_answer"
    assert terminal.data["status"] == "partial"
    assert terminal.data["reason"] == "missing_final_answer"
    assert error.data["recoverable"] is False
    assert [event.type for event in events].index("agent.run.completed") < [
        event.type for event in events
    ].index("done")
