from __future__ import annotations

from backend.ws.stream_state import (
    apply_stream_event,
    create_stream_state,
    get_stream_content_blocks,
    upsert_pending_tool_call,
)


def _project(
    state: dict,
    event_type: str,
    payload: dict,
) -> dict:
    projected = apply_stream_event(
        {"conv-1": state},
        "conv-1",
        event_type,
        payload,
    )
    assert projected is state
    return state


def _tool_block(state: dict, tool_id: str) -> dict:
    return next(
        block["record"]
        for block in state["content_blocks"]
        if block.get("type") == "tool_call"
        and block.get("record", {}).get("id") == tool_id
    )


def test_create_stream_state_uses_complete_resume_contract_shape() -> None:
    state = create_stream_state("conv-1", "assistant-1")

    assert state == {
        "conversation_id": "conv-1",
        "message_id": "assistant-1",
        "turn_id": "",
        "content_blocks": [],
        "phase": "",
        "status": "running",
        "event_seq": 0,
        "last_event_type": "",
        "last_event_at": 0,
        # Only the canonical `done` event fences delivery for the live turn;
        # retryable errors must not freeze the resume snapshot.
        "terminal_fenced": False,
        "tool_calls": {},
    }


def test_agent_message_lifecycle_projects_one_authoritative_block() -> None:
    state = create_stream_state("conv-1", "assistant-1")
    _project(state, "item.started", {
        "item": {"id": "agent-message", "type": "agent_message", "status": "in_progress", "text": ""},
    })
    _project(state, "agent_message.delta", {
        "item_id": "agent-message",
        "delta": "Hello ",
    })
    _project(state, "agent_message.delta", {
        "item_id": "agent-message",
        "delta": "world",
    })
    _project(state, "item.completed", {
        "item": {
            "id": "agent-message",
            "type": "agent_message",
            "text": "Hello world",
            "source": "model_final",
            "status": "completed",
        },
    })

    assert state["phase"] == "final"
    assert state["content_blocks"][0] == {
        "type": "text",
        "itemId": "agent-message",
        "content": "Hello world",
        "source": "model_final",
        "status": "completed",
        "isStreaming": False,
    }


def test_provisional_source_is_ignored_until_agent_message_completion() -> None:
    state = create_stream_state("conv-1", "assistant-1")
    _project(state, "item.started", {
        "item": {
            "id": "agent-message",
            "type": "agent_message",
            "source": "model_final",
        },
    })
    _project(state, "agent_message.delta", {
        "item_id": "agent-message",
        "delta": "I will inspect the files first.",
        "source": "model_final",
    })

    assert "source" not in state["content_blocks"][0]
    assert state["phase"] == "model"

    _project(state, "item.completed", {
        "item": {
            "id": "agent-message",
            "type": "agent_message",
            "text": "I will inspect the files first.",
            "source": "commentary",
            "status": "completed",
        },
    })

    assert state["content_blocks"][0]["source"] == "commentary"
    assert state["phase"] == "model"


def test_cancelled_tool_result_stays_cancelled_in_reconnect_snapshot() -> None:
    state = create_stream_state("conv-1", "assistant-1")
    _project(state, "tool_call", {
        "id": "tc-cancelled",
        "name": "run_command",
        "args": {"command": "long-task"},
    })
    _project(state, "tool_result", {
        "id": "tc-cancelled",
        "name": "run_command",
        "status": "cancelled",
        "is_error": True,
        "summary": "Command cancelled",
    })

    assert state["tool_calls"]["tc-cancelled"]["status"] == "cancelled"
    assert _tool_block(state, "tc-cancelled")["status"] == "cancelled"


def test_distinct_agent_message_items_close_without_restarting_an_id() -> None:
    state = create_stream_state("conv-1", "assistant-1")
    first_id = "iter-1:agent-message:1"
    second_id = "iter-1:agent-message:2"
    _project(state, "item.started", {"item": {"id": first_id, "type": "agent_message", "text": ""}})
    _project(state, "agent_message.delta", {"item_id": first_id, "delta": "rejected"})
    _project(state, "item.completed", {"item": {
        "id": first_id,
        "type": "agent_message",
        "text": "rejected",
        "source": "cancelled",
        "status": "cancelled",
    }})
    _project(state, "item.started", {"item": {"id": second_id, "type": "agent_message", "text": ""}})
    _project(state, "agent_message.delta", {"item_id": second_id, "delta": "accepted"})
    _project(state, "item.completed", {
        "item": {
            "id": second_id,
            "type": "agent_message",
            "text": "accepted",
            "source": "model_final",
            "status": "completed",
        },
    })

    assert len(state["content_blocks"]) == 2
    assert state["content_blocks"][0]["status"] == "cancelled"
    assert state["content_blocks"][1]["content"] == "accepted"


def test_ordered_snapshot_keeps_typed_thinking_process_tool_and_answer() -> None:
    state = create_stream_state("conv-1", "assistant-1")
    _project(state, "thinking_delta", {
        "content": "checking",
        "source": "provider",
        "visibility": "timeline",
        "phase": "model",
    })
    _project(state, "agent.item", {
        "id": "item-1",
        "kind": "observation",
        "content": "Inspecting files",
        "visibility": "timeline",
        "iteration_id": "iter-1",
    })
    _project(state, "tool_call", {
        "id": "tc-1",
        "name": "read_file",
        "args": {"path": "README.md"},
        "iteration_id": "iter-1",
    })
    _project(state, "agent.progress", {
        "id": "progress-1",
        "stage": "tool",
        "phase": "tool",
        "status": "running",
        "message": "Reading README",
        "tool_call_id": "tc-1",
        "tool_name": "read_file",
        "iteration_id": "iter-1",
    })
    _project(state, "item.completed", {
        "item": {
            "id": "agent-message",
            "type": "agent_message",
            "text": "Done.",
            "source": "model_final",
            "status": "completed",
        },
    })

    assert [block["type"] for block in state["content_blocks"]] == [
        "thinking",
        "process",
        "tool_call",
        "text",
    ]
    assert set(state["tool_calls"]) == {"tc-1"}
    assert state["event_seq"] == 5
    assert state["last_event_type"] == "item.completed"


def test_tool_lifecycle_restores_waiting_output_and_terminal_state_without_resurrection() -> None:
    state = create_stream_state("conv-1", "assistant-1")
    _project(state, "tool_call", {
        "id": "tc-1",
        "name": "run_command",
        "args": {"command": "build"},
        "started_at": 100,
    })
    _project(state, "approval_request", {
        "tool_call_id": "tc-1",
        "tool_name": "run_command",
        "args": {"command": "build"},
        "waiting_on": "user",
        "blocking_reason": "approval_required",
    })
    waiting = state["tool_calls"]["tc-1"]
    assert waiting["status"] == "pending"
    assert waiting["transition"] == "waiting_approval"
    assert waiting["waitingOn"] == "user"
    assert waiting["blockingReason"] == "approval_required"
    assert state["phase"] == "approval"

    _project(state, "tool_output_delta", {
        "id": "tc-1",
        "output": "line 1\n",
        "stream": "stdout",
    })
    _project(state, "tool_output_delta", {
        "id": "tc-1",
        "output": "failure\n",
        "stream": "stderr",
    })
    _project(state, "tool_result", {
        "id": "tc-1",
        "name": "run_command",
        "status": "blocked",
        "is_error": True,
        "summary": "Command blocked",
        "duration_ms": 42,
        "error_info": {"kind": "permission"},
    })
    terminal = dict(state["tool_calls"]["tc-1"])
    assert terminal["status"] == "blocked"
    assert terminal["transition"] == "blocked"
    assert terminal["durationMs"] == 42
    assert terminal["stdoutPreview"] == "line 1\n"
    assert terminal["stderrPreview"] == "failure\n"
    assert terminal["finishedAt"] > 0
    assert _tool_block(state, "tc-1") == terminal

    _project(state, "runtime.span", {
        "event": "tool.started",
        "tool_call_id": "tc-1",
        "tool_name": "run_command",
        "status": "running",
    })
    _project(state, "agent.progress", {
        "id": "late-progress",
        "stage": "tool",
        "phase": "tool",
        "status": "running",
        "message": "Late progress",
        "tool_call_id": "tc-1",
    })
    _project(state, "runtime.span", {
        "event": "tool.completed",
        "tool_call_id": "tc-1",
        "tool_name": "run_command",
        "status": "completed",
        "data": {},
    })

    restored = state["tool_calls"]["tc-1"]
    assert restored["status"] == "blocked"
    assert restored["transition"] == "blocked"
    assert restored["finishedAt"] == terminal["finishedAt"]
    assert set(state["tool_calls"]) == {"tc-1"}
    assert _tool_block(state, "tc-1")["status"] == "blocked"


def test_runtime_span_maps_each_tool_transition_and_terminal_result_can_refine_it() -> None:
    state = create_stream_state("conv-1", "assistant-1")
    expected = (
        ("tool.preparing", "pending", "prepared"),
        ("tool.queued", "pending", "queued"),
        ("approval.waiting", "pending", "waiting_approval"),
        ("tool.started", "running", "running"),
        ("tool.first_output", "running", "streaming_output"),
        ("tool.completed", "success", "completed"),
    )
    for event, status, transition in expected:
        _project(state, "runtime.span", {
            "event": event,
            "tool_call_id": "tc-runtime",
            "tool_name": "grep_files",
            "data": {"tool_status": "success"} if event == "tool.completed" else {},
        })
        assert state["tool_calls"]["tc-runtime"]["status"] == status
        assert state["tool_calls"]["tc-runtime"]["transition"] == transition

    _project(state, "tool_result", {
        "id": "tc-runtime",
        "name": "grep_files",
        "status": "failed",
        "is_error": True,
        "summary": "Search failed",
    })
    assert state["tool_calls"]["tc-runtime"]["status"] == "failed"
    assert state["tool_calls"]["tc-runtime"]["transition"] == "failed"


def test_snapshot_is_an_isolated_copy() -> None:
    state = create_stream_state("conv-1", "assistant-1")
    _project(state, "item.completed", {
        "item": {
            "id": "agent-message",
            "type": "agent_message",
            "text": "answer",
            "status": "completed",
        },
    })

    snapshot = get_stream_content_blocks(state)
    snapshot[0]["content"] = "changed"
    assert state["content_blocks"][0]["content"] == "answer"


def test_pending_tool_upsert_keeps_tool_map_and_content_blocks_in_sync() -> None:
    state = {"conversation_id": "conv-1", "message_id": "assistant-1"}

    pending = upsert_pending_tool_call(
        state,
        "tc-1",
        {"id": "tc-1", "name": "read_file", "args": {}, "status": "running"},
    )
    assert pending["tc-1"]["name"] == "read_file"
    assert _tool_block(state, "tc-1")["name"] == "read_file"


def test_done_and_error_project_terminal_stream_status() -> None:
    completed = create_stream_state("conv-1", "assistant-1")
    _project(completed, "done", {"status": "partial", "reason": "max_iterations"})
    assert completed["status"] == "partial"
    assert completed["terminal_reason"] == "max_iterations"

    failed = create_stream_state("conv-1", "assistant-2")
    _project(failed, "error", {"message": "provider failed"})
    assert failed["status"] == "failed"
