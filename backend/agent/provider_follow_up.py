"""Continue a completed native response that has not ended the user turn."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from backend.agent.message import AgentEvent
from backend.agent.response_utils import append_assistant_history


def retain_provider_follow_up(*, state: Any, stream_state: Any, stream_text: Any, context_builder: Any) -> Iterator[AgentEvent]:
    text = stream_text.full_text
    if stream_text.final_candidate_item_id and stream_text.saw_final_answer_phase:
        yield AgentEvent.agent_message_completed(
            text=stream_text.final_candidate_text,
            item_id=stream_text.final_candidate_item_id,
            source="commentary",
            status="completed",
            finish_reason=stream_state.finish_reason,
        )
    else:
        completed = stream_text.complete_active_agent_message(text, source="commentary", status="completed")
        if completed is not None:
            yield completed
    append_assistant_history(context_builder, text, phase=stream_state.response_phase, provider_items=stream_state.response_items)
    state.mark_transition("provider_follow_up")
    stream_text.reset_for_retry()
    stream_text.saw_final_answer_phase = False
