from __future__ import annotations

from backend.agent.message import AgentEvent
from backend.llm.base import ToolCallEvent


def model_process_text_event(
    content: str,
    tool_calls: list[ToolCallEvent],
    *,
    iteration_id: str,
    source: str,
    status: str = "completed",
) -> AgentEvent | None:
    text = (content or "").strip()
    if not text:
        return None
    # Unphased text with no tool call may be provider/internal analysis. Once it
    # directly leads into a concrete tool call, preserve it as process narration
    # after the provisional answer draft is retracted.
    if (source or "model_preamble") == "model_preamble":
        return None
    return AgentEvent.agent_item(
        id=f"{iteration_id}:model-output:{source or 'stream'}",
        kind="process_text",
        content=text,
        loop_id=iteration_id,
        iteration_id=iteration_id,
        role="assistant",
        source=source or "model_preamble",
        status=status,
        title="Model output",
        summary=text,
        visibility="timeline",
        group_id=iteration_id,
        step_id=f"{iteration_id}:model-output",
        tool_call_ids=[tc.id for tc in tool_calls],
        display_scope="activity",
        seq=1,
    )
