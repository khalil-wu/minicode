from __future__ import annotations

import asyncio

from backend.agent.context import ContextBuilder
from backend.agent.loop_recovery import emergency_compact


def test_emergency_compact_treats_noop_as_failure() -> None:
    """A nothing-compactable compaction must not count as recovery success."""

    class ShortHistoryBuilder(ContextBuilder):
        def __init__(self) -> None:
            super().__init__()
            from backend.llm.base import LLMMessage

            self._history = [LLMMessage(role="user", content="hi")]

    async def scenario() -> bool:
        builder = ShortHistoryBuilder()
        from backend.agent.state import AgentState

        return await emergency_compact(AgentState(user_message="x"), builder)

    assert asyncio.run(scenario()) is False
