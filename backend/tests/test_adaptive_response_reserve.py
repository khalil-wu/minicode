"""Small windows must leave room for the real tool/prompt floor and history."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend import config as config_module
from backend.agent.context import ContextBuilder
from backend.agent.state import AgentState
from backend.config import AppConfig, LLMSettings, TokenBudget
from backend.llm.base import LLMMessage, estimate_llm_context_tokens
from backend.llm.model_selection import config_with_model_budget
from backend.services.context_budget import manage_context_budget


@pytest.mark.parametrize("window", [28_000, 32_768, 36_000])
def test_small_window_keeps_post_compaction_prompt_without_recompacting(window):
    budget = TokenBudget(total=window)
    ctx = ContextBuilder(token_budget=budget)
    state = AgentState(user_message="Continue the coding task")
    # Reproduce the 19-20K irreducible prompt from the live 36K failure.
    messages = [LLMMessage(role="user", content="x" * 80_000)]
    used = estimate_llm_context_tokens(messages)
    assert 19_000 <= used <= 21_000
    ctx.compact = AsyncMock(side_effect=AssertionError("prompt already fits"))

    async def run():
        for _ in range(4):
            events = [event async for event in manage_context_budget(
                ctx, state, budget, [], messages=messages,
            )]
            assert all(event.type not in {"context_compacted", "error"} for event in events)
        assert not state.stopped_reason

    asyncio.run(run())
    ctx.compact.assert_not_called()
    assert budget.history_budget >= 10_000
    # The same measured prompt still compacts at the exact configured boundary.
    ctx.bind_budget(replace(budget, total=used + 100, response_reserve=100))
    assert not ctx.needs_compaction(state, messages=messages)
    ctx.bind_budget(replace(budget, total=used + 99, response_reserve=100))
    assert ctx.needs_compaction(state, messages=messages)


def test_auto_reserve_follows_model_switches_in_both_directions():
    runtime = SimpleNamespace(get_model=lambda _provider, model: SimpleNamespace(context_window=int(model)))
    original = AppConfig(llm=LLMSettings(api_key=""), token_budget=TokenBudget(total=200_000))
    small = config_with_model_budget(original, model_runtime=runtime, provider="custom", model="36000")
    large = config_with_model_budget(small, model_runtime=runtime, provider="custom", model="200000")
    assert small.token_budget.reserved_response_tokens == 3_600
    assert large.token_budget.reserved_response_tokens == 16_384
    assert original.token_budget.total == 200_000
    assert small.token_budget.response_reserve is None


@pytest.mark.parametrize("reserve", [0, 2_048, 16_384])
def test_explicit_reserve_is_preserved_on_model_switch(reserve):
    runtime = SimpleNamespace(get_model=lambda *_: SimpleNamespace(context_window=36_000))
    config = AppConfig(llm=LLMSettings(api_key=""), token_budget=TokenBudget(response_reserve=reserve))
    selected = config_with_model_budget(config, model_runtime=runtime, provider="custom", model="small")
    assert selected.token_budget.reserved_response_tokens == reserve


@pytest.mark.parametrize("settings, expected", [({}, 3_600), ({"response_reserve": None}, 3_600), ({"response_reserve": 0}, 0), ({"response_reserve": 2_048}, 2_048)])
def test_config_projection_retains_auto_and_explicit_reserves(monkeypatch, settings, expected):
    stack = SimpleNamespace(
        effective_config=lambda: {"token_budget": {"total": 36_000, **settings}},
        requirements=SimpleNamespace(feature_requirements={}),
    )
    monkeypatch.setattr(config_module, "load_config_layer_stack", lambda **_: stack)
    monkeypatch.setattr(config_module, "load_llm_settings", lambda _: LLMSettings(api_key=""))
    budget = config_module.load_config().token_budget
    assert budget.reserved_response_tokens == expected
