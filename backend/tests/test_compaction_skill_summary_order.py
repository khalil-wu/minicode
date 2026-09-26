"""Incremental compaction survives an invoked skill.

Compacted history keeps admitted user inputs ahead of the summary, and the
re-injected skill fragment must not come between them: the next compaction
finds the previous summary by skipping user inputs only.
"""

from __future__ import annotations

import asyncio

from backend.agent.context import ContextBuilder, _extract_compaction_summary
from backend.agent.state import AgentState
from backend.config import AgentSettings
from backend.llm.base import LLMAdapter, LLMMessage, StreamEvent, StreamEventType


def _summary(label: str) -> str:
    return (
        f"## Goal\n{label}\n\n## Constraints & Preferences\n- keep\n\n"
        "## Progress\n- done\n\n## Key Decisions\n- k\n\n## Next Steps\n1. go\n\n"
        f"## Critical Context\n- {label}"
    )


class _SummaryLLM(LLMAdapter):
    def __init__(self) -> None:
        self.calls: list[list[LLMMessage]] = []

    async def stream_chat(self, messages, tools=None):
        if False:
            yield StreamEvent(type=StreamEventType.DONE)

    async def simple_chat(self, messages, *, max_tokens=None):
        self.calls.append(messages)
        return _summary(f"SUMMARY-{len(self.calls)}")


async def _turns(builder: ContextBuilder, start: int, *, skill: bool = False) -> None:
    for index in range(start, start + 10):
        state = AgentState(user_message=f"task-{index} " + "x" * 200)
        if skill and index == start:
            state.prompt_context["skill_injections"] = [
                {"name": "review", "path": "C:/skills/review/SKILL.md", "content": "Review workflow body"}
            ]
        await builder.start_turn(state.user_message, state)
        builder.append_assistant(f"answer-{index} " + "y" * 200)


def test_second_compaction_updates_the_previous_summary_after_a_skill() -> None:
    llm = _SummaryLLM()
    builder = ContextBuilder(llm=llm, agent_settings=AgentSettings(compaction_keep_recent_tokens=40))

    async def scenario() -> None:
        await _turns(builder, 0, skill=True)
        await builder.compact()
        await _turns(builder, 10)
        await builder.compact()

    asyncio.run(scenario())

    layout = []
    for message in builder._history:
        content = str(message.content or "")
        if content.startswith("<skill>"):
            layout.append("skill")
        elif _extract_compaction_summary(content) is not None:
            layout.append("summary")
    assert layout[:2] == ["summary", "skill"]
    second_prompt = str(llm.calls[1][-1].content)
    assert "<previous-summary>" in second_prompt
    assert "SUMMARY-1" in second_prompt
