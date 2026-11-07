from copy import deepcopy
from dataclasses import replace
from unittest.mock import Mock

import pytest

from backend.agent.context import ContextBuilder
from backend.agent.state import AgentState
from backend.config import TokenBudget
from backend.llm.base import LLMMessage, ToolCallEvent, estimate_llm_context_tokens, estimate_llm_message_tokens


def test_rendered_budget_reuses_retained_history_and_preserves_signed_usage_correction(monkeypatch):
    builder = ContextBuilder(token_budget=TokenBudget(total=2_000_000))
    for index in range(10_000):
        builder._history_store.append(LLMMessage(role="user", content=f"retained message {index}"))
    plugin = LLMMessage(role="developer", content="Installed plugin instructions")
    builder._history_store.append(plugin)
    messages = [LLMMessage(role="system", content="system"), LLMMessage(role="developer", content="runtime"), *builder._history]
    tools = [{"type": "function", "function": {"name": "read_file", "parameters": {"type": "object"}}}]
    expected = estimate_llm_context_tokens(messages, tools)
    builder._observed_prompt_estimate = 1000
    builder._last_actual_prompt_tokens = 850
    estimate = Mock(wraps=builder._estimate_history_message)
    monkeypatch.setattr(builder, "_estimate_history_message", estimate)
    monkeypatch.setattr(builder, "_build_plugin_instructions", lambda state: plugin.content)
    snapshot = builder.get_budget_snapshot(AgentState(user_message="continue"), tools, messages=messages)
    assert snapshot["used"] == expected - 150
    assert snapshot["breakdown"]["plugins"] == estimate_llm_message_tokens(plugin)
    assert snapshot["breakdown"]["history"] == sum(estimate_llm_message_tokens(message) for message in builder._history[:-1])
    assert [call.args[0] for call in estimate.call_args_list] == messages[:2]


def test_budget_counts_media_projection_and_repaired_tools_from_actual_request(monkeypatch):
    builder = ContextBuilder(token_budget=TokenBudget(total=50_000))
    image = LLMMessage(role="user", content="see image", images=[{"data": "encoded", "media_type": "image/png"}])
    call = LLMMessage(role="assistant", content="", tool_calls=[ToolCallEvent(id="read", name="read_file", arguments={"path": "a.txt"})])
    builder._history_store.append(image)
    builder._history_store.append(call)
    builder._history_store.append(LLMMessage(role="user", content="continue after interrupted read"))
    retained = deepcopy(builder._history)
    projected = builder._get_history_within_budget()
    projected[0] = replace(image, images=[], content="image unavailable to this model")
    expected = estimate_llm_context_tokens(projected)
    estimate = Mock(wraps=builder._estimate_history_message)
    monkeypatch.setattr(builder, "_estimate_history_message", estimate)
    snapshot = builder.get_budget_snapshot(AgentState(user_message="continue"), messages=projected)
    assert snapshot["used"] == expected
    assert builder._history == retained
    assert len(estimate.call_args_list) == 2
    assert estimate.call_args_list[0].args[0] is projected[0]
    assert estimate.call_args_list[1].args[0].tool_call_id == "read"


@pytest.mark.parametrize("change", ["append", "replace", "rewrite", "clear", "restore"])
def test_budget_follows_canonical_history_mutations_and_detached_extension_content(change):
    builder = ContextBuilder(token_budget=TokenBudget(total=50_000))
    builder.append_user("original input")
    builder.append_assistant("original answer")
    builder.get_budget_snapshot(AgentState(user_message=""), messages=list(builder._history))
    if change == "append":
        builder.append_user("more input" * 30)
    elif change == "replace":
        builder._history_store.replace_all([LLMMessage(role="user", content="compacted summary" * 20)])
    elif change == "rewrite":
        builder._history[0].content = "rewritten retained input" * 25
        builder._history_store.refresh_message_estimate(builder._history[0])
    elif change == "clear":
        builder.clear()
    else:
        builder.load_snapshot(builder.export_snapshot())
    projected = [*builder._history, LLMMessage(role="developer", content="current instruction")]
    # Context hooks own detached replacements, even when derived from history.
    if builder._history:
        projected[0] = replace(builder._history[0], content="extension transformed input" * 50)
    snapshot = builder.get_budget_snapshot(AgentState(user_message=""), messages=projected)
    assert snapshot["used"] == estimate_llm_context_tokens(projected)
