from __future__ import annotations

import asyncio
from types import SimpleNamespace

from backend.agent.context import ContextBuilder
from backend.agent.error_withholding import ErrorWithholdingController
from backend.agent.loop_recovery import emergency_compact, strip_historical_media, try_error_withholding_recovery


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

        controller = ErrorWithholdingController()
        recovered = await try_error_withholding_recovery(
            error_controller=controller,
            classification=SimpleNamespace(provider_error_type="", error_type="context_overflow"),
            error_content="prompt is too long",
            state=AgentState(user_message="x"),
            context=builder,
            compact=emergency_compact,
            strip_media=strip_historical_media,
        )
        assert controller.recovery_log[0]["success"] is False
        assert controller.recovery_log[0]["detail"] == "Nothing to compact"
        return recovered

    assert asyncio.run(scenario()) is False
