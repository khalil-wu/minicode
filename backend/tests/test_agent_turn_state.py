from backend.agent.turn_state import AgentTurnState

import pytest


def _running_state() -> AgentTurnState:
    state = AgentTurnState(now_ms=lambda: 1234)
    state.record_progress({
        "id": "progress-1",
        "stage": "tool",
        "status": "running",
        "message": "Running tool",
    })
    state.record_process_item({
        "id": "process-1",
        "kind": "process_text",
        "status": "running",
        "content": "Working",
    })
    state.record_tool_call({
        "id": "tool-1",
        "name": "read_file",
        "args": {"file_path": "README.md"},
        "status": "running",
    })
    return state


def test_provider_progress_state_remains_a_string_in_snapshot() -> None:
    state = AgentTurnState(now_ms=lambda: 1234)
    state.record_progress({
        "id": "provider:request-1",
        "stage": "status",
        "status": "running",
        "message": "模型正在响应",
        "provider_state": "responding",
        "retry_attempt": 1,
    })

    progress = state.finalize(terminal_status="partial").blocks[0]

    assert progress["providerState"] == "responding"
    assert progress["retryAttempt"] == 1


def test_progress_metadata_uses_completion_time_only_for_subagent_rows() -> None:
    state = AgentTurnState(now_ms=lambda: 1234)
    completion = {"id": "subagent-completed:child:2", "stage": "status", "phase": "subagent",
                  "status": "completed", "message": "Reviewer已完成", "subagent_id": "child",
                  "subagent_name": "Reviewer", "subagent_identity": "/root/reviewer", "subagent_status": "done",
                  "timestamp": "2026-10-06T05:00:00+00:00"}
    state.record_progress(completion)
    state.record_progress(completion)
    state.record_progress({"id": "ordinary", "stage": "status", "status": "info", "message": "Working",
                           "timestamp": "2026-10-06T05:00:00+00:00"})
    blocks = state.finalize(terminal_status="completed").blocks
    assert len(blocks) == 2
    assert blocks[0]["timestamp"] == 1_791_262_800_000
    assert blocks[0]["subagentStatus"] == "done"
    assert blocks[1]["timestamp"] == 1234


def test_cancelled_turn_persists_unfinished_tool_as_cancelled() -> None:
    snapshot = _running_state().finalize(terminal_status="cancelled")

    progress = next(block for block in snapshot.blocks if block.get("type") == "progress")
    process = next(block for block in snapshot.blocks if block.get("type") == "process")
    tool = snapshot.tool_calls[0]
    assert progress["status"] == "partial"
    assert process["status"] == "partial"
    assert tool["status"] == "cancelled"
    assert "terminationReason" not in tool


def test_interrupted_turn_uses_the_same_cancelled_tool_status() -> None:
    snapshot = _running_state().finalize(terminal_status="interrupted")

    tool = snapshot.tool_calls[0]
    assert tool["status"] == "cancelled"
    assert "terminationReason" not in tool


def test_partial_turn_keeps_unfinished_tool_as_partial() -> None:
    snapshot = _running_state().finalize(terminal_status="partial")

    tool = snapshot.tool_calls[0]
    assert tool["status"] == "partial"
    assert "terminationReason" not in tool


def test_failed_turn_persists_unfinished_work_as_failed() -> None:
    snapshot = _running_state().finalize(terminal_status="failed")

    progress = next(block for block in snapshot.blocks if block.get("type") == "progress")
    process = next(block for block in snapshot.blocks if block.get("type") == "process")
    tool = snapshot.tool_calls[0]
    assert progress["status"] == "failed"
    assert process["status"] == "failed"
    assert tool["status"] == "failed"
    assert tool["terminationReason"] == "missing_tool_result"


def test_completed_turn_closes_progress_but_fails_missing_tool_result() -> None:
    snapshot = _running_state().finalize(terminal_status="completed")

    progress = next(block for block in snapshot.blocks if block.get("type") == "progress")
    process = next(block for block in snapshot.blocks if block.get("type") == "process")
    assert progress["status"] == "completed"
    assert process["status"] == "completed"
    assert snapshot.tool_calls[0]["status"] == "failed"
    assert snapshot.tool_calls[0]["terminationReason"] == "missing_tool_result"


@pytest.mark.parametrize("terminal_status", ["cancelled", "failed", "partial"])
@pytest.mark.parametrize("source", ["commentary", "pending", None])
def test_interrupted_narration_never_becomes_a_partial_answer(terminal_status, source):
    state = AgentTurnState(now_ms=lambda: 1234)
    state.start_agent_message("narration", {"source": source} if source else None)
    state.append_agent_message_delta("narration", "I will inspect the repository.")
    snapshot = state.finalize(terminal_status=terminal_status)
    assert snapshot.content == ""
    assert snapshot.blocks[0]["source"] == (source or "pending")
    assert snapshot.blocks[0]["content"] == "I will inspect the repository."
    assert snapshot.blocks[0]["isStreaming"] is False


@pytest.mark.parametrize("source", ["model_final", "reply", "partial"])
def test_interrupted_answer_candidate_keeps_usable_partial_answer(source):
    state = AgentTurnState(now_ms=lambda: 1234)
    state.start_agent_message("answer", {"source": source})
    state.append_agent_message_delta("answer", "Verified result")
    snapshot = state.finalize(terminal_status="cancelled")
    assert snapshot.content == "Verified result"
    assert snapshot.blocks[0]["source"] == "partial"
