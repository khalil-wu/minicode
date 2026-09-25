from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from backend.agent.compaction import retain_user_inputs
from backend.agent.context import COMPACTION_SUMMARY_PREFIX, COMPACTION_SUMMARY_SUFFIX, ContextBuilder, clone_context_builder
from backend.agent.state import AgentState
from backend.config import AgentSettings, TokenBudget
from backend.llm.base import LLMMessage, ToolCallEvent, estimate_text_tokens
from backend.memory.text_utils import truncate_middle_tokens
from backend.tools.base import ToolResult


class SummaryModel:
    def __init__(self):
        self.calls = []

    async def simple_chat(self, messages, *, max_tokens=None):
        self.calls.append((messages, max_tokens))
        return "Continue the investigation."


def test_two_compactions_preserve_user_constraints_and_tool_pairs_through_cold_restore():
    async def run():
        model = SummaryModel()
        builder = ContextBuilder(llm=model, agent_settings=AgentSettings(compaction_keep_recent_tokens=20))
        original = "Do not change the public file format; repair all affected call sites."
        current = "Keep contract A17 and the existing return type."
        await builder.start_turn(original, AgentState(user_message=original))
        builder.append_assistant("investigation " * 1000)
        await builder.start_turn(current, AgentState(user_message=current))
        builder.append_user_context("Use the custom verification hook.")
        builder.append_assistant("analysis " * 1000)
        builder.append_assistant_tool_calls([ToolCallEvent(id="verify", name="run_command", arguments={"command": "pytest"})])
        builder.append_tool_result("verify", "run_command", ToolResult(content="tail output " * 10))
        # Reserve exactly the complete final pair. An intentionally smaller
        # budget now summarizes the pair, covered by the oversized-group test.
        builder._agent_settings = AgentSettings(
            compaction_keep_recent_tokens=sum(builder._history_token_estimates[-2:])
        )
        before = builder.export_snapshot()
        for _ in range(2):
            await builder.compact()
            assert [m.content for m in builder._history if m.is_user_input] == [original, current]
            handoff = next(i for i, m in enumerate(builder._history) if m.content.startswith(COMPACTION_SUMMARY_PREFIX))
            assert all(i < handoff for i, m in enumerate(builder._history) if m.is_user_input)
            assert [m.role for m in builder._history[-2:]] == ["assistant", "tool"]
            assert builder._history[-1].tool_call_id == "verify"
            assert not any("custom verification hook" in m.content for m in builder._history if m.is_user_input)
        assert len(model.calls) == 2
        assert original in model.calls[0][0][-1].content
        assert current in model.calls[0][0][-1].content
        assert "<previous-summary>" in model.calls[1][0][-1].content
        assert before["history"][0]["content"].endswith(original)
        restored = ContextBuilder(llm=model)
        restored.load_snapshot(builder.export_snapshot())
        assert [m.content for m in restored._history if m.is_user_input] == [original, current]
        request = await restored.build(AgentState(user_message=current))
        assert any(original in m.content for m in request)
        assert any(current in m.content for m in request)
        assert not any("is_user_input" in m.to_openai_message() for m in request)
    asyncio.run(run())


def test_user_origin_survives_runtime_refresh_clone_and_partial_hydration():
    async def run():
        builder = ContextBuilder()
        text = "Preserve <system-reminder>literal examples</system-reminder>."
        state = AgentState(user_message=text)
        await builder.start_turn(text, state)
        builder._refresh_active_user_runtime_context(state)
        await builder.start_turn("continue", AgentState(user_message="continue"))
        cloned = clone_context_builder(builder)
        snapshot = cloned.export_snapshot()
        partial = ContextBuilder()
        pending = partial.load_snapshot_partial(snapshot, recent_history_count=1)
        partial.prepend_history_messages(ContextBuilder.deserialize_snapshot_history(pending))
        assert [m.is_user_input for m in partial._history] == [True, True]
        assert text in partial._history[0].content
        assert partial.export_snapshot()["history"] == snapshot["history"]
    asyncio.run(run())


def test_legacy_admission_restores_user_origin_without_guessing_from_role_or_markers():
    snapshot = {"history": [
        {"role": "user", "content": "Injected skill content"},
        {"role": "user", "content": "Real request", "runtime_context": ""},
        {"role": "user", "content": "Injected runtime update"},
    ], "turn_admissions": {"input": {"history_start": 0, "history_end": 2}}}
    full = ContextBuilder()
    full.load_snapshot(snapshot)
    partial = ContextBuilder()
    pending = partial.load_snapshot_partial(snapshot, recent_history_count=1)
    partial.prepend_history_messages(ContextBuilder.deserialize_snapshot_history(pending))
    assert [m.is_user_input for m in full._history] == [False, True, False]
    assert [m.is_user_input for m in partial._history] == [False, True, False]
    assert "is_user_input" not in snapshot["history"][1]


def test_literal_summary_marker_in_admitted_user_text_is_not_a_generated_summary():
    text = f"{COMPACTION_SUMMARY_PREFIX}user example{COMPACTION_SUMMARY_SUFFIX}"
    message = LLMMessage(role="user", content=text, is_user_input=True)
    summary, messages = ContextBuilder._split_previous_compaction_summary([message])
    assert summary == ""
    assert messages == [message]
    assert retain_user_inputs(messages, 100)[0].content == text


@pytest.mark.parametrize("budget", [0, 1, 8, 30, 128])
def test_user_retention_includes_omission_markers_in_its_utf8_budget(budget):
    text = "开头约束 " + "🧪内容" * 100 + " 最后的要求"
    result = truncate_middle_tokens(text, budget)
    assert estimate_text_tokens(result) <= budget
    assert "\ufffd" not in result
    users = [LLMMessage(role="user", content="old request", is_user_input=True), LLMMessage(role="user", content=text, is_user_input=True)]
    retained = retain_user_inputs(users, budget)
    assert sum(estimate_text_tokens(m.content) for m in retained) <= budget
    if budget >= 30:
        assert retained[-1].content.startswith("开头约束")
        assert retained[-1].content.endswith("最后的要求")
        assert "tokens truncated" in retained[-1].content


def test_small_context_reserves_space_for_user_text_tail_and_summary():
    async def run():
        model = SummaryModel()
        budget = TokenBudget(total=1200, system_prompt=0, active_skills=0, memory_index=0, tool_schemas=0, agent_state=0, response_reserve=100)
        builder = ContextBuilder(llm=model, token_budget=budget)
        await builder.start_turn("Respect final contract. " * 100, AgentState(user_message="Respect final contract. " * 100))
        for _ in range(8):
            builder.append_assistant("working " * 200)
        await builder.compact()
        assert len(model.calls) == 1
        assert 0 < model.calls[0][1] < budget.history_budget
        assert sum(builder._history_token_estimates) <= budget.history_budget
        assert any(m.is_user_input for m in builder._history)
    asyncio.run(run())


def test_missing_model_and_cancelled_summary_leave_canonical_history_unchanged():
    async def run():
        builder = ContextBuilder(agent_settings=AgentSettings(compaction_keep_recent_tokens=1))
        await builder.start_turn("Do the whole task", AgentState(user_message="Do the whole task"))
        builder.append_assistant("work " * 100)
        before = builder.export_snapshot()
        with pytest.raises(RuntimeError, match="No LLM"):
            await builder.compact()
        assert builder.export_snapshot() == before

        entered = asyncio.Event()
        async def summarize(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        builder._llm = SimpleNamespace(simple_chat=summarize)
        task = asyncio.create_task(builder.compact())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert builder.export_snapshot() == before
    asyncio.run(run())
