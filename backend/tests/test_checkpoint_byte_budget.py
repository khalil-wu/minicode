"""A checkpoint whose tool history exceeds the byte budget stays resumable."""

from __future__ import annotations

from pathlib import Path

from backend.agent.checkpoint import _fit_checkpoint_payload, load_latest_checkpoint
from backend.agent.context import ContextBuilder
from backend.agent.query_recovery import prepare_query_recovery
from backend.agent.run_context import RunContext
from backend.agent.runtime import AgentRuntime
from backend.agent.state import AgentState
from backend.agent.turn_kernel import TurnKernel
from backend.config import TokenBudget


def _long_interrupted_state(conversation_id: str, reads: int) -> AgentState:
    state = AgentState(user_message="fix", conversation_id=conversation_id, stopped_reason="interrupted")
    for index in range(reads):
        state.record_tool_call(
            "read_file",
            {"file_path": f"pkg/mod_{index}.py"},
            "line of source\n" * 3000,
            content_preview="preview " * 50,
        )
    return state


def test_budgeted_payload_keeps_identity_and_whole_records() -> None:
    record = {"tool_name": "read_file", "tool_input": {"file_path": "a.py"}, "tool_output": "x" * 30_000}
    payload = {
        "session_id": "session-1",
        "run_id": "run-1",
        "conversation_id": "conversation-1",
        "stopped_reason": "interrupted",
        "resume_payload": {"run_id": "run-1", "conversation_id": "conversation-1"},
        "user_message": "fix",
        "reply": "",
        "context_snapshot": {"history": []},
        "messages": [],
        "tool_calls": [dict(record) for _ in range(120)],
        "active_skills": [],
        "disabled_tools": [],
        "loaded_deferred_tools": [],
    }
    fitted = _fit_checkpoint_payload(payload)

    assert fitted["conversation_id"] == "conversation-1"
    assert fitted["run_id"] == "run-1"
    assert fitted["stopped_reason"] == "interrupted"
    assert fitted["resume_payload"] == payload["resume_payload"]
    assert 0 < len(fitted["tool_calls"]) < 120
    assert all(set(item) == set(record) for item in fitted["tool_calls"])


def test_long_interrupted_turn_checkpoint_is_found_and_resumes(tmp_path: Path) -> None:
    runtime = AgentRuntime(
        metrics_file=tmp_path / "metrics.jsonl",
        swarm_store_dir=tmp_path / "swarm",
        enable_lease_heartbeat=False,
    )
    try:
        context = ContextBuilder(token_budget=TokenBudget())
        context.append_user("Read the whole package and fix the bug.")
        state = _long_interrupted_state("conversation-long", reads=80)
        kernel = TurnKernel.create(
            metadata={}, state=state, budget=TokenBudget(), task_id="task", session_id="session",
            emit_event=None, initial_user_message="fix", run_context=RunContext(agent_runtime=runtime),
        )
        assert kernel.finalize_checkpoint(
            session_id="session", user_message="fix", state=state, context_builder=context,
        ) == "saved"

        checkpoint = load_latest_checkpoint("session", conversation_id="conversation-long", base_dir=runtime.state_root)
        assert checkpoint is not None
        assert checkpoint.stopped_reason == "interrupted"
        assert checkpoint.run_id == kernel.run_record.run_id
        assert checkpoint.tool_calls and all("tool_name" in record for record in checkpoint.tool_calls)

        resumed = prepare_query_recovery(
            session_id="session",
            conversation_id="conversation-long",
            metadata={"resume_from_checkpoint": True},
            state=AgentState(user_message="continue"),
            context_builder=ContextBuilder(token_budget=TokenBudget()),
            max_iterations_budget=0,
            current_run_id="run-next",
            checkpoint_base_dir=runtime.state_root,
        )
        assert resumed.restored
    finally:
        runtime.close(release_lease=True)
