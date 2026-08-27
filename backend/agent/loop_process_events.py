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
    if not text or not any(character.isalnum() for character in text):
        return None
    item_id = f"{iteration_id}:model-output:{source or 'stream'}"
    # A tool call is the hard boundary that proves unphased assistant text was
    # mid-turn narration rather than the final answer. Preserve that text just
    # like pi/Claude Code preserve assistant text before a tool-use block.
    return AgentEvent.agent_item(
        id=item_id,
        kind="process_text",
        content=text,
        loop_id=iteration_id,
        iteration_id=iteration_id,
        role="assistant",
        source=source or "model_preamble",
        status=status,
        title="Model output",
        summary=(
            f"Model output before {len(tool_calls)} tool call"
            f"{'s' if len(tool_calls) != 1 else ''}"
            if tool_calls
            else "Model process output"
        ),
        visibility="timeline",
        group_id=iteration_id,
        step_id=f"{iteration_id}:model-output",
        tool_call_ids=[tc.id for tc in tool_calls],
        order=1,
    )
