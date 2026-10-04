import asyncio

import pytest

from backend.llm.anthropic_adapter import AnthropicAdapter
from backend.llm.base import (
    LLMMessage, LLMSideCallContext, SideQueryOptions, StreamEvent, StreamEventType,
)
from backend.llm.provider_contracts import ReasoningPolicy


def capture_request(adapter, *, options=None):
    captured = []

    async def stream(payload, **_kwargs):
        captured.append(payload)
        yield StreamEvent(type=StreamEventType.DONE, finish_reason="end_turn")

    adapter._stream_messages = stream

    async def collect():
        context = LLMSideCallContext(options=options, record={}) if options else None
        async for _ in adapter._stream_chat_with_context(
            [LLMMessage(role="user", content="hello")], context=context,
        ):
            pass

    asyncio.run(collect())
    return captured[0]


@pytest.mark.parametrize("selection", ["constructor", "session"])
def test_explicit_off_disables_provider_default_thinking(selection):
    adapter = AnthropicAdapter(
        "fixture", model="glm-5.3-flash", max_tokens=2000,
        thinking_budget=1234, reasoning_effort="off" if selection == "constructor" else "high",
    )
    if selection == "session":
        adapter.apply_reasoning_policy(ReasoningPolicy(level="off"))
    payload = capture_request(adapter)
    assert adapter.current_reasoning_effort() == "off"
    assert payload["thinking"] == {"type": "disabled"}
    assert "output_config" not in payload
    assert "extra_headers" not in payload


def test_unset_keeps_provider_default():
    adapter = AnthropicAdapter("fixture", model="glm-5.3-flash")
    assert "thinking" not in capture_request(adapter)


def test_high_can_be_restored_after_off():
    adapter = AnthropicAdapter(
        "fixture", model="glm-5.3-flash", max_tokens=2000,
        thinking_budget=1234, reasoning_effort="off",
    )
    adapter.apply_reasoning_policy(ReasoningPolicy(level="high", wire_level="high"))
    payload = capture_request(adapter)
    assert payload["thinking"] == {"type": "enabled", "budget_tokens": 1234}
    assert payload["output_config"]["effort"] == "high"


@pytest.mark.parametrize("small_model", [False, True])
def test_side_query_disables_thinking_without_changing_model(small_model):
    adapter = AnthropicAdapter(
        "fixture", model="claude-opus-4-6", small_fast_model="claude-sonnet-4-6",
        max_tokens=2000, thinking_budget=1234, reasoning_effort="high",
    )
    payload = capture_request(adapter, options=SideQueryOptions(
        operation="probe", disable_reasoning=True, use_small_fast_model=small_model,
    ))
    assert payload["thinking"] == {"type": "disabled"}
    assert "output_config" not in payload
    assert payload["model"] == ("claude-sonnet-4-6" if small_model else "claude-opus-4-6")
