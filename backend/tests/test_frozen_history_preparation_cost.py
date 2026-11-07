from backend.agent.context import ContextBuilder
from backend.llm.base import LLMMessage


def test_runtime_wrapper_cleanup_visits_only_unsent_tail_and_keeps_frozen_evidence(monkeypatch):
    builder = ContextBuilder()
    builder._history = [LLMMessage(role="user", content=f"frozen {index}") for index in range(1000)]
    builder._history_frozen_count = len(builder._history)
    frozen = list(builder._history)
    builder._history_store.append(LLMMessage(role="user", content="<system-reminder>\nold\n</system-reminder>\n\nold input", runtime_context="old", is_user_input=True))
    builder._history_store.append(LLMMessage(role="user", content="<system-reminder>\ncurrent\n</system-reminder>\n\ncurrent input", runtime_context="current", is_user_input=True))
    strip = builder._strip_trusted_runtime_context
    visited = []

    def observe(message):
        assert not message.content.startswith("frozen ")
        visited.append(message)
        return strip(message)

    monkeypatch.setattr(builder, "_strip_trusted_runtime_context", observe)
    assert builder._compact_old_user_runtime_context_for_cache(keep_recent_user_turns=1) == 1
    assert len(visited) <= 4
    assert all(before is after for before, after in zip(frozen, builder._history))
    assert builder._history[-2].content == "old input"
    assert builder._history[-1].runtime_context == "current"
    assert builder._history_frozen_count == len(frozen)
