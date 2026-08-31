from __future__ import annotations

from types import SimpleNamespace

from backend.agent.answer_committer import AnswerCommitDependencies, AnswerCommitter
from backend.agent.answer_commit_projection import AnswerCommitProjection
from backend.agent.answer_commit_projection import build_answer_commit_projection
from backend.agent.stream_attempt import StreamAttemptState, StreamTextState
from backend.agent.loop_process_events import model_process_text_event
from backend.llm.base import StreamEvent, StreamEventType, ToolCallEvent, UsageInfo


def test_agent_message_items_close_before_a_new_unique_item_starts() -> None:
    state = StreamTextState(iteration_id="iteration:4")

    first_events = state.project_agent_message_delta("rejected")
    first_id = first_events[0].data["item"]["id"]

    assert first_events[0].type == "item.started"
    assert first_events[1].type == "agent_message.delta"
    assert first_events[1].data["item_id"] == first_id

    cancelled = state.cancel_active_agent_message()
    assert cancelled is not None
    assert cancelled.type == "item.completed"
    assert cancelled.data["item"] == {
        "id": first_id,
        "type": "agent_message",
        "text": "rejected",
        "source": "cancelled",
        "status": "cancelled",
    }

    second_events = state.project_agent_message_delta("accepted")
    second_id = second_events[0].data["item"]["id"]
    assert second_id != first_id
    assert second_events[1].data["item_id"] == second_id

    completed = state.complete_active_agent_message(
        "accepted",
        source="model_final",
        status="completed",
    )
    assert completed is not None
    assert completed.data["item"]["id"] == second_id
    assert completed.data["item"]["text"] == "accepted"


def test_answer_committer_carries_finish_reason_into_done_provider_metadata() -> None:
    state = SimpleNamespace(stopped_reason="", reply="")

    def set_terminal_reason(target, reason, *, status):
        target.stopped_reason = reason
        target.terminal_status = status

    committer = AnswerCommitter(
        AnswerCommitDependencies(
            context=SimpleNamespace(),
            state=state,
            append_assistant_history=lambda *_args, **_kwargs: None,
            set_terminal_reason=set_terminal_reason,
        )
    )
    source_raw = {"provider": "custom"}

    projection = committer.commit_answer(
        projection=AnswerCommitProjection(
            text_events=(),
            terminal_reason="completed",
            terminal_status="completed",
        ),
        final_text="answer",
        provider_phase="final_answer",
        provider_items=[],
        usage=UsageInfo(input_tokens=2, output_tokens=1),
        provider_raw=source_raw,
        finish_reason="end_turn",
    )

    event = projection.to_event(status="completed", reason="")
    assert event.type == "done"
    assert event.data["provider_raw"]["finish_reason"] == "end_turn"
    assert "finish_reason" not in source_raw


def test_streamed_agent_message_projects_source_from_the_first_frame() -> None:
    state = StreamTextState(iteration_id="iteration:5")

    events = state.project_agent_message_delta("checking", source="pending")

    assert state.active_agent_message_source == "pending"
    assert events[0].data["item"]["source"] == "pending"
    # The item start already published the classification, so repeating it on
    # every token would only inflate the stream.
    assert "source" not in events[1].data

    unchanged = state.project_agent_message_delta(" more", source="pending")
    assert "source" not in unchanged[0].data

    completed = state.complete_active_agent_message(
        "checking",
        source="commentary",
        status="completed",
    )

    assert completed is not None
    assert completed.data["item"]["source"] == "commentary"


def test_streamed_agent_message_republishes_source_on_reclassification() -> None:
    # A provider may relabel the same assistant item mid-stream. That single
    # transition is the only delta that needs to carry a source, and the
    # renderer relies on it to move live text out of the work log.
    state = StreamTextState(iteration_id="iteration:6")

    state.project_agent_message_delta("thinking about it", source="pending")
    upgraded = state.project_agent_message_delta(" the answer", source="model_final")

    assert [event.type for event in upgraded] == ["agent_message.delta"]
    assert upgraded[0].data["source"] == "model_final"
    assert "source" not in state.project_agent_message_delta(
        " continues", source="model_final"
    )[0].data


def test_process_text_projection_emits_each_changed_accumulated_prefix() -> None:
    state = StreamTextState(iteration_id="iteration:process")
    state.pending_process_text = "first"

    first = state.maybe_stream_process_text(
        source="commentary", event_factory=model_process_text_event
    )
    duplicate = state.maybe_stream_process_text(
        source="commentary", event_factory=model_process_text_event
    )
    state.pending_process_text = "first second"
    second = state.maybe_stream_process_text(
        source="commentary", event_factory=model_process_text_event
    )

    assert first is not None
    assert first.data["content"] == "first"
    assert duplicate is None
    assert second is not None
    assert second.data["content"] == "first second"


def test_stream_attempt_preserves_duplicate_provider_ids_by_batch_occurrence() -> None:
    state = StreamAttemptState()
    first = ToolCallEvent(
        id="duplicate",
        name="read_file",
        arguments={"file_path": "a.py"},
    )
    second = ToolCallEvent(
        id="duplicate",
        name="read_file",
        arguments={"file_path": "b.py"},
    )

    state.accept_provider_event(
        StreamEvent(
            type=StreamEventType.TOOL_CALL,
            tool_calls=[first, second],
        )
    )

    assert [call.arguments for call in state.tool_calls] == [
        {"file_path": "a.py"},
        {"file_path": "b.py"},
    ]

    state.accept_provider_event(
        StreamEvent(
            type=StreamEventType.TOOL_CALL,
            tool_calls=[
                ToolCallEvent(
                    id="duplicate",
                    name="read_file",
                    arguments={"file_path": "a-final.py"},
                ),
                ToolCallEvent(
                    id="duplicate",
                    name="read_file",
                    arguments={"file_path": "b-final.py"},
                ),
            ],
        )
    )

    assert [call.arguments for call in state.tool_calls] == [
        {"file_path": "a-final.py"},
        {"file_path": "b-final.py"},
    ]


def test_stream_text_uses_provider_item_identity_for_distinct_message_items() -> None:
    state = StreamTextState(iteration_id="iteration")

    first = state.project_agent_message_delta(
        "commentary",
        source="commentary",
        item_id="provider-message-1",
    )
    second = state.project_agent_message_delta(
        "answer",
        source="model_final",
        item_id="provider-message-2",
    )

    assert first[0].data["item"]["id"] == "provider-message-1"
    assert second[0].type == "item.completed"
    assert second[0].data["item"]["id"] == "provider-message-1"
    assert second[1].data["item"]["id"] == "provider-message-2"
    assert second[2].data["item_id"] == "provider-message-2"


def test_answer_commit_completes_only_the_active_provider_item_text() -> None:
    state = StreamTextState(iteration_id="iteration")
    state.project_agent_message_delta(
        "first",
        source="model_final",
        item_id="provider-message-1",
    )
    second = state.project_agent_message_delta(
        "second",
        source="model_final",
        item_id="provider-message-2",
    )
    state.final_candidate_text = "firstsecond"
    state.final_candidate_item_id = "provider-message-2"

    projection = build_answer_commit_projection(
        stream_text=state,
        final_text=state.final_candidate_text,
        finish_reason="stop",
        provider_raw={},
        degraded_reason="",
    )

    assert second[0].data["item"]["text"] == "first"
    assert projection.text_events[-1].data["item"]["id"] == "provider-message-2"
    assert projection.text_events[-1].data["item"]["text"] == "second"


def test_answer_commit_redacts_provider_refusal_explanation_from_public_item() -> None:
    state = StreamTextState(iteration_id="iteration")
    state.final_candidate_text = "safe answer"

    projection = build_answer_commit_projection(
        stream_text=state,
        final_text="safe answer",
        finish_reason="stop",
        provider_raw={
            "provider": "anthropic",
            "refusal": {
                "type": "refusal",
                "category": "policy",
                "explanation": "DO_NOT_PERSIST_REFUSAL_TEXT",
            },
        },
        degraded_reason="",
    )

    provider_raw = projection.text_events[-1].data["provider_raw"]
    assert provider_raw["refusal"] == {
        "type": "refusal",
        "category": "policy",
        "explanation_available": True,
    }
    assert "DO_NOT_PERSIST_REFUSAL_TEXT" not in str(provider_raw)
