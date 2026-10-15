"""One actual run clock owns admission, partial snapshots and terminal delivery."""
from __future__ import annotations

import ast
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.agent import runtime as runtime_module, runtime_records
from backend.agent.message import AgentEvent
from backend.agent.context import ContextBuilder
from backend.agent.execution_journal import execution_journal_owner
from backend.agent.query_journal import QueryJournalRecorder
from backend.agent.query_terminal import QueryTerminalTransaction
from backend.agent.query_engine import QueryEngine, QuerySetupError
from backend.agent.run_context import RunContext
from backend.agent.runtime import AgentRuntime
from backend.agent.runtime_records import _agent_run_from_dict
from backend.agent.turn_state import AgentTurnState
from backend.agent.turn_kernel import TurnKernel
from backend.agent.state import AgentState
from backend.ws import agent_runner


@pytest.fixture
def clock_owner(tmp_path, monkeypatch):
    clock = {"monotonic": 50.0, "epoch_ms": 200_000}
    monkeypatch.setattr(runtime_module, "monotonic", lambda: clock["monotonic"])
    monkeypatch.setattr(runtime_records, "monotonic", lambda: clock["monotonic"])
    monkeypatch.setattr(runtime_records, "epoch_ms", lambda: clock["epoch_ms"])
    runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl", swarm_store_dir=tmp_path / "swarm",
        enable_lease_heartbeat=False)
    record = runtime.start_run(run_id="original-clock", conversation_id="clock-conversation", session_id="clock-window")
    clock["epoch_ms"] = record.started_at
    yield clock, runtime, record
    runtime.close(release_lease=True)


def callbacks(namespace):
    source = ast.parse(Path(agent_runner.__file__).read_text(encoding="utf-8"))
    functions = [node for node in ast.walk(source) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in {"_partial_turn_message", "_send_done_once"}]
    assignment = next(node for node in ast.walk(source) if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "terminal_duration_ms" for target in node.targets))
    factory = ast.parse("def make_callbacks():\n    done_event_sent = False\n").body[0]
    factory.body.extend([assignment, *functions])
    factory.body.append(ast.Return(value=ast.Call(func=ast.Name(id="locals", ctx=ast.Load()), args=[], keywords=[])))
    module = ast.fix_missing_locations(ast.Module(body=[factory], type_ignores=[]))
    env = {**vars(agent_runner), **namespace}
    exec(compile(module, agent_runner.__file__, "exec"), env)
    return env["make_callbacks"]()


def test_original_admission_clock_survives_setup_reentry_and_terminal_persistence(clock_owner):
    clock, runtime, original = clock_owner
    started = AgentEvent.agent_run_started(original).to_ws_message()
    assert started["started_at"] == original.started_at
    assert "duration_ms" not in started
    clock.update(monotonic=51.125, epoch_ms=original.started_at + 1125)
    assert runtime.start_run(run_id=original.run_id, conversation_id=original.conversation_id,
        session_id=original.session_id) is original
    assert original.elapsed_ms() == 1125
    clock.update(monotonic=107.25, epoch_ms=original.started_at + 57_250)
    terminal = runtime.commit_terminal(original.run_id)
    assert terminal.duration_ms == terminal.elapsed_ms() == 57_250
    assert AgentEvent.agent_run_completed(terminal).data["duration_ms"] == 57_250
    persisted = runtime._swarm_store.get_agent_run(original.run_id)
    assert persisted["duration_ms"] == 57_250
    assert "_started_monotonic" not in persisted
    cold = _agent_run_from_dict(persisted)
    clock.update(monotonic=999.0, epoch_ms=999_000)
    assert cold.elapsed_ms() == terminal.elapsed_ms() == 57_250
    assert cold._started_monotonic is None


def test_restored_running_record_does_not_invent_a_new_monotonic_origin(clock_owner):
    clock, _runtime, original = clock_owner
    restored = _agent_run_from_dict(original.to_dict())
    assert restored.started_at == original.started_at
    clock.update(monotonic=80.0, epoch_ms=230_000)
    assert restored.elapsed_ms() is None
    assert "duration_ms" not in restored.complete().to_dict()
    epoch_origin = _agent_run_from_dict({**original.to_dict(), "started_at": 0})
    assert epoch_origin.started_at == 0
    assert epoch_origin.elapsed_ms() is None


def test_wall_clock_adjustment_does_not_replace_measured_elapsed_time(clock_owner):
    clock, runtime, original = clock_owner
    clock.update(monotonic=53.5, epoch_ms=900_000)
    terminal = runtime.commit_terminal(original.run_id)
    assert terminal.completed_at == 900_000
    assert terminal.duration_ms == 3500
    assert terminal.duration_ms != terminal.completed_at - terminal.started_at


@pytest.mark.asyncio
@pytest.mark.parametrize("known_clock", [True, False])
async def test_actual_ws_partial_and_done_use_original_run_measurement(clock_owner, known_clock):
    clock, runtime, original = clock_owner
    sent = []
    async def send(event):
        sent.append(event.to_ws_message())
    state = AgentTurnState(now_ms=lambda: clock["epoch_ms"])
    state.append_thinking("actual public work", {"source": "harness"})
    run = RunContext(agent_runtime=runtime)
    clock.update(monotonic=55.5, epoch_ms=original.started_at + 5500)
    namespace = {
        "self": SimpleNamespace(send_event=send), "run_context": run,
        "run_metadata": {"run_id": original.run_id}, "turn_state": state,
        "assistant_message_id": "original-assistant", "assistant_artifacts": [], "latest_turn_diff": None,
        # These later viewer/setup clocks are deliberately unrelated.
        "turn_started_at_ms": 201_125, "_now_ms": lambda: 999_000,
        "query_done_payload": {"status": "completed", "duration_ms": 999},
        "query_run_completed_payload": {}, "conversation": SimpleNamespace(id="clock-conversation"),
        "_owns_query_claim": lambda: True, "_terminal_delivery_complete": lambda: False,
        "turn_completion_notification": None, "run_failure_recoverable": None,
    }
    partial = callbacks(namespace)["_partial_turn_message"]()
    assert partial["duration_ms"] == 5500
    assert partial["timestamp"] == datetime.fromtimestamp(original.started_at / 1000, UTC).isoformat()
    clock.update(monotonic=107.25, epoch_ms=original.started_at + 57_250)
    terminal = runtime.commit_terminal(original.run_id)
    completion = AgentEvent.agent_run_completed(terminal).data
    if not known_clock:
        completion = {key: value for key, value in completion.items() if key != "duration_ms"}
    namespace["query_run_completed_payload"] = completion
    assert await callbacks(namespace)["_send_done_once"]()
    assert len(sent) == 1
    if known_clock:
        assert sent[0]["duration_ms"] == completion["duration_ms"] == 57_250
    else:
        assert "duration_ms" not in sent[0]


@pytest.mark.asyncio
async def test_actual_terminal_transaction_publishes_and_journals_the_same_measured_duration(clock_owner):
    clock, runtime, original = clock_owner
    state = AgentState(user_message="original request", reply="real final answer")
    builder = ContextBuilder()
    run = RunContext(agent_runtime=runtime)
    metadata = {"run_id": original.run_id, "conversation_id": original.conversation_id,
        "assistant_message_id": "original-assistant"}
    kernel = TurnKernel(runtime=runtime, run_record=original, metadata=metadata,
        emit_event=None, initial_user_message=state.user_message, state=state, run_context=run)
    journal = runtime.execution_journal(execution_journal_owner("conversation", "clock-test", original.conversation_id))
    recorder = QueryJournalRecorder(journal, metadata, state, builder, kernel, original.conversation_id)
    ctx = SimpleNamespace(state=state, context_builder=builder, run_context=run, turn_kernel=kernel,
        metadata=metadata, session_id=original.session_id, user_message=state.user_message)
    transaction = QueryTerminalTransaction(ctx, recorder)
    clock.update(monotonic=107.25, epoch_ms=original.started_at + 57_250)
    result = await transaction.commit(AgentEvent(type="done", data={"status": "completed", "duration_ms": 999}), validate=True)
    assert result.terminal_event.data["duration_ms"] == result.completion_event.data["duration_ms"] == 57_250
    assert not result.evidence_events
    completed = [event for event in journal.read_events() if event.payload.get("lifecycle") == "runtime_terminal_committed"]
    assert len(completed) == 1
    assert completed[0].payload["duration_ms"] == 57_250


def test_actual_startup_failure_uses_the_original_clock_and_same_durable_receipt(clock_owner):
    clock, runtime, original = clock_owner
    clock.update(monotonic=55.5, epoch_ms=original.started_at + 5500)
    completed = AgentEvent.agent_run_completed(runtime.commit_terminal(original.run_id, "failed", terminal_reason="startup_failed"))
    journal = runtime.execution_journal(execution_journal_owner("conversation", "clock-test", original.conversation_id))
    run = RunContext(agent_runtime=runtime, execution_journal=journal)
    error = QuerySetupError(ValueError("actual setup failure"), completion_event=completed,
        metadata={"run_id": original.run_id}, state=AgentState(user_message="original request"),
        conversation_id=original.conversation_id, run_context=run)
    events = QueryEngine()._setup_failure_events(SimpleNamespace(runtime=SimpleNamespace(task_id="clock-task")), error)
    assert events[-1].type == "done"
    assert events[-1].data["duration_ms"] == completed.data["duration_ms"] == 5500
    receipt = next(event for event in journal.read_events() if event.payload.get("lifecycle") == "runtime_terminal_committed")
    assert receipt.payload["duration_ms"] == 5500
