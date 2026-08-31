"""Regression coverage for side-query model selection and usage accounting."""

from __future__ import annotations

import asyncio
import json
import math
from types import SimpleNamespace

import pytest

from backend.config import LLMSettings
from backend.llm.anthropic_adapter import AnthropicAdapter
from backend.llm.base import (
    LLMAdapter,
    LLMMessage,
    LLMSideCallContext,
    LLMTurnContext,
    SideQueryOptions,
    StreamEvent,
    StreamEventType,
    UsageInfo,
)
from backend.llm.cost_tracker import CostTracker
from backend.llm.openai_adapter import OpenAIAdapter
from backend.agent.turn_budget_runtime import TurnBudgetRuntime


class _AnthropicResponse:
    status_code = 200
    headers = {"content-type": "text/event-stream"}

    def __init__(self, lines):
        self.lines = lines
    async def __aenter__(self): return self
    async def __aexit__(self, *exc): return False
    async def aiter_lines(self):
        for line in self.lines: yield line
    async def aread(self): return b""
    def raise_for_status(self): return None


class _AnthropicClient:
    def __init__(self, captured): self.captured = captured
    def stream(self, method, url, **kwargs):
        self.captured.update(kwargs)
        body = kwargs.get("json")
        if isinstance(body, dict): self.captured.update(body)
        events = [
            {"type": "message_start", "message": {"id": "msg-side-query", "usage": {"input_tokens": 10, "output_tokens": 0}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "condensed"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 2}},
            {"type": "message_stop"},
        ]
        return _AnthropicResponse([f"data: {json.dumps(event)}" for event in events])


def setup_function() -> None:
    CostTracker.get_instance().reset()


def teardown_function() -> None:
    CostTracker.get_instance().reset()


async def _openai_completed_stream(
    text: str = "condensed",
    *,
    input_tokens: int = 20,
    output_tokens: int = 4,
):
    yield SimpleNamespace(
        type="response.completed",
        response=SimpleNamespace(
            output_text=text,
            output=[],
            usage=SimpleNamespace(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            ),
        ),
    )


async def _anthropic_completed_stream(
    text: str = "condensed",
    *,
    input_tokens: int = 10,
    output_tokens: int = 2,
):
    yield SimpleNamespace(
        type="message_start",
        message=SimpleNamespace(
            id="msg-side-query",
            usage=SimpleNamespace(
                input_tokens=input_tokens,
                output_tokens=0,
            ),
            stop_reason="",
        ),
    )
    yield SimpleNamespace(
        type="content_block_start",
        index=0,
        content_block=SimpleNamespace(type="text"),
    )
    yield SimpleNamespace(
        type="content_block_delta",
        index=0,
        delta=SimpleNamespace(type="text_delta", text=text),
    )
    yield SimpleNamespace(type="content_block_stop", index=0)
    yield SimpleNamespace(
        type="message_delta",
        delta=SimpleNamespace(stop_reason="end_turn"),
        usage=SimpleNamespace(output_tokens=output_tokens),
    )
    yield SimpleNamespace(type="message_stop")


def test_record_non_stream_usage_parses_responses_cache_and_reasoning() -> None:
    usage = {
        "input_tokens": 100,
        "output_tokens": 20,
        "input_tokens_details": {"cached_tokens": 40},
        "output_tokens_details": {"reasoning_tokens": 7},
    }
    bucket = UsageInfo()
    turn_context = LLMTurnContext(usage=bucket)
    LLMAdapter.record_non_stream_usage(
        usage,
        provider="openai",
        model_id="gpt-test",
        input_includes_cache_read=True,
        context=LLMSideCallContext(
            options=SideQueryOptions(operation="usage_test"),
            record={},
            turn=turn_context,
        ),
    )

    totals = CostTracker.get_instance().get_summary()
    assert totals["input_tokens"] == 0
    assert totals["output_tokens"] == 0
    assert totals["cache_read_tokens"] == 0
    assert totals["reasoning_output_tokens"] == 0
    assert bucket.input_tokens == 100
    assert bucket.output_tokens == 20
    assert bucket.cache_read_input_tokens == 40
    assert bucket.reasoning_output_tokens == 7


def test_record_non_stream_usage_parses_chat_cache_fields() -> None:
    usage = {
        "prompt_cache_hit_tokens": 30,
        "prompt_cache_miss_tokens": 70,
        "completion_tokens": 12,
    }
    LLMAdapter.record_non_stream_usage(
        usage,
        provider="custom",
        model_id="compatible-chat-model",
        input_includes_cache_read=True,
    )
    totals = CostTracker.get_instance().get_summary()
    assert totals["input_tokens"] == 100
    assert totals["output_tokens"] == 12
    assert totals["cache_read_tokens"] == 30


def test_record_non_stream_usage_none_is_noop() -> None:
    LLMAdapter.record_non_stream_usage(
        None,
        provider="openai",
        model_id="x",
        input_includes_cache_read=True,
    )
    totals = CostTracker.get_instance().get_summary()
    assert totals["input_tokens"] == 0
    assert totals["output_tokens"] == 0


def test_cost_tracker_separates_session_totals() -> None:
    tracker = CostTracker.get_instance()
    tracker.record_usage(10, 2, session_id="session-a", model_id="gpt-5.4")
    tracker.record_usage(20, 4, session_id="session-b", model_id="gpt-5.4")

    session_a = tracker.get_summary("session-a")
    session_a["uptime_sec"] = 0
    assert session_a == {
        "scope": "session",
        "session_id": "session-a",
        "input_tokens": 10,
        "ordinary_input_tokens": 10,
        "output_tokens": 2,
        "cache_creation_tokens": 0,
        "cache_read_tokens": 0,
        "prompt_cache_total_tokens": 10,
        "reasoning_output_tokens": 0,
        # gpt-5.4 is not in the local price table and OpenAI reports no cost,
        # so the request is unpriced. total_cost_usd is a subtotal (0.0 priced
        # requests) and cost_complete says so rather than claiming $0 is real.
        "total_cost_usd": 0.0,
        "priced_requests": 0,
        "unpriced_requests": 1,
        "cost_complete": False,
        "total_duration_sec": 0.0,
        "uptime_sec": 0,
    }
    assert tracker.get_summary("session-b")["input_tokens"] == 20
    assert tracker.get_summary()["input_tokens"] == 30


def test_cost_tracker_ignores_malformed_provider_deltas() -> None:
    tracker = CostTracker.get_instance()
    tracker.record_usage(
        input_tokens="not-a-count",
        output_tokens=math.nan,
        cache_read_input_tokens=-10,
        elapsed_sec="not-a-duration",
        cost_usd="not-a-cost",
    )

    summary = tracker.get_summary()
    assert summary["input_tokens"] == 0
    assert summary["output_tokens"] == 0
    assert summary["cache_read_tokens"] == 0
    assert summary["total_cost_usd"] == 0.0
    assert summary["total_duration_sec"] == 0.0


def test_cost_tracker_preserves_authoritative_mixed_provider_prompt_totals() -> None:
    tracker = CostTracker.get_instance()
    tracker.record_usage(
        input_tokens=900,
        output_tokens=30,
        cache_read_input_tokens=800,
        cache_creation_input_tokens=300,
        # OpenAI request: ordinary 600 / read 200 / write 0.
        # Anthropic request: ordinary 100 / read 600 / write 300.
        ordinary_input_tokens=700,
        prompt_cache_total_tokens=1_800,
        # The aggregate contains a provider whose cache counters are separate;
        # these booleans alone cannot reconstruct the mixed total.
        input_includes_cache_read=False,
        input_includes_cache_write=False,
    )

    summary = tracker.get_summary()
    assert summary["input_tokens"] == 900
    assert summary["ordinary_input_tokens"] == 700
    assert summary["cache_read_tokens"] == 800
    assert summary["cache_creation_tokens"] == 300
    assert summary["prompt_cache_total_tokens"] == 1_800


def test_turn_usage_ignores_malformed_non_stream_provider_counters() -> None:
    bucket = UsageInfo()
    turn_context = LLMTurnContext(usage=bucket)
    LLMAdapter.record_non_stream_usage(
        {
            "input_tokens": -5,
            "output_tokens": "not-a-count",
            "cache_read_input_tokens": float("inf"),
            "cost_usd": "not-a-cost",
        },
        provider="gateway",
        model_id="gateway-model",
        input_includes_cache_read=True,
        context=LLMSideCallContext(
            options=SideQueryOptions(operation="usage_test"),
            record={},
            turn=turn_context,
        ),
    )

    assert bucket.input_tokens == 0
    assert bucket.output_tokens == 0
    assert bucket.cache_read_input_tokens == 0
    assert bucket.cost_usd == 0.0


def test_turn_cost_runtime_reads_turn_owned_provider_cost_before_terminal_commit() -> None:
    runtime = TurnBudgetRuntime.__new__(TurnBudgetRuntime)
    runtime.cost_session_id = "conversation-a"
    turn_usage = UsageInfo(cost_usd=1.25)
    runtime.usage = lambda: turn_usage

    assert runtime.turn_cost_usd() == 1.25


def test_record_non_stream_usage_accumulates_explicit_provider_cost() -> None:
    bucket = UsageInfo()
    turn_context = LLMTurnContext(usage=bucket)
    LLMAdapter.record_non_stream_usage(
        {"input_tokens": 1, "cost_usd": 0.75},
        provider="gateway",
        model_id="gateway-model",
        input_includes_cache_read=True,
        context=LLMSideCallContext(
            options=SideQueryOptions(operation="usage_test"),
            record={},
            turn=turn_context,
        ),
    )

    assert bucket.cost_usd == 0.75


def test_record_non_stream_usage_preserves_provider_cache_accounting_mode() -> None:
    bucket = UsageInfo(input_includes_cache_read=True)
    turn_context = LLMTurnContext(usage=bucket)
    LLMAdapter.record_non_stream_usage(
        {
            "input_tokens": 100,
            "output_tokens": 10,
            "input_tokens_details": {"cached_tokens": 25},
        },
        provider="anthropic",
        model_id="claude",
        input_includes_cache_read=False,
        context=LLMSideCallContext(
            options=SideQueryOptions(operation="usage_test"),
            record={},
            turn=turn_context,
        ),
    )

    assert bucket.input_includes_cache_read is False
    assert bucket.billable_tokens == 110


class _TelemetryAdapter(LLMAdapter):
    async def stream_chat(self, messages, tools=None, metadata=None):
        del messages, tools, metadata
        yield StreamEvent(type=StreamEventType.DONE)

    async def simple_chat(self, messages, *, max_tokens=None) -> str:
        del messages, max_tokens
        return "ok"

    async def _side_query_chat(self, messages, *, context) -> str:
        del messages
        self.annotate_side_call(context, provider="test", model_id="test-fast")
        self.record_non_stream_usage(
            {
                "input_tokens": 12,
                "output_tokens": 3,
                "input_tokens_details": {"cached_tokens": 2},
                "output_tokens_details": {"reasoning_tokens": 1},
            },
            provider="test",
            model_id="test-fast",
            input_includes_cache_read=True,
            context=context,
        )
        return "ok"


def test_side_query_records_operation_usage_and_elapsed_in_turn_sink() -> None:
    adapter = _TelemetryAdapter()
    turn_usage = UsageInfo()
    records: list[dict] = []
    turn_context = LLMTurnContext(
        usage=turn_usage,
        side_call_records=records,
    )
    result = asyncio.run(
        adapter.side_query(
            [LLMMessage(role="user", content="summarize")],
            options=SideQueryOptions(
                operation="web_fetch_apply",
                max_tokens=256,
                use_small_fast_model=True,
                disable_reasoning=True,
                enable_prompt_cache=False,
            ),
            turn_context=turn_context,
        )
    )

    assert result == "ok"
    assert turn_usage.input_tokens == 12
    assert turn_usage.output_tokens == 3
    assert len(records) == 1
    record = records[0]
    assert record["operation"] == "web_fetch_apply"
    assert record["provider"] == "test"
    assert record["model"] == "test-fast"
    assert record["status"] == "completed"
    assert record["elapsed_ms"] >= 0
    assert record["usage"]["cache_read_input_tokens"] == 2
    assert record["usage"]["ordinary_input_tokens"] == 10
    assert record["usage"]["prompt_cache_total_tokens"] == 12
    assert record["usage"]["reasoning_output_tokens"] == 1


def test_side_query_retries_transient_error_with_same_session_identity(monkeypatch) -> None:
    class _RetryingAdapter(LLMAdapter):
        def __init__(self) -> None:
            self.calls = 0
            self.session_ids: list[str] = []

        async def stream_chat(self, messages, tools=None, metadata=None):
            del messages, tools, metadata
            yield StreamEvent(type=StreamEventType.DONE)

        async def simple_chat(self, messages, *, max_tokens=None) -> str:
            del messages, max_tokens
            return "prompt-processed result"

        async def _side_query_chat(self, messages, *, context) -> str:
            del messages
            self.calls += 1
            options = context.options
            self.session_ids.append(options.session_id)
            if self.calls == 1:
                raise ConnectionError("peer closed connection without complete message body")
            return "prompt-processed result"

    sleep_delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleep_delays.append(delay)

    monkeypatch.setattr("backend.llm.base.asyncio.sleep", fake_sleep)
    monkeypatch.setattr("backend.llm.base.random.random", lambda: 0.0)
    adapter = _RetryingAdapter()
    records: list[dict] = []
    options = SideQueryOptions(operation="web_fetch_apply")
    turn_context = LLMTurnContext(side_call_records=records)
    result = asyncio.run(
        adapter.side_query(
            [LLMMessage(role="user", content="page")],
            options=options,
            turn_context=turn_context,
        )
    )

    assert result == "prompt-processed result"
    assert adapter.calls == 2
    assert adapter.session_ids == [options.session_id, options.session_id]
    assert sleep_delays == [0.5]
    assert records[0]["status"] == "completed"
    assert records[0]["attempts"] == 2
    assert records[0]["retry_count"] == 1


def test_side_query_does_not_retry_fatal_provider_error(monkeypatch) -> None:
    class _FatalAdapter(LLMAdapter):
        def __init__(self) -> None:
            self.calls = 0

        async def stream_chat(self, messages, tools=None, metadata=None):
            del messages, tools, metadata
            yield StreamEvent(type=StreamEventType.DONE)

        async def simple_chat(self, messages, *, max_tokens=None) -> str:
            del messages, max_tokens
            self.calls += 1
            raise RuntimeError("HTTP 401 invalid api key")

    async def unexpected_sleep(_delay: float) -> None:
        raise AssertionError("fatal errors must not be retried")

    monkeypatch.setattr("backend.llm.base.asyncio.sleep", unexpected_sleep)
    adapter = _FatalAdapter()

    with pytest.raises(RuntimeError, match="401"):
        asyncio.run(
            adapter.side_query(
                [LLMMessage(role="user", content="page")],
                options=SideQueryOptions(operation="web_fetch_apply"),
            )
        )

    assert adapter.calls == 1


def test_web_fetch_side_query_does_not_amplify_background_529(monkeypatch) -> None:
    class _BusyAdapter(LLMAdapter):
        def __init__(self) -> None:
            self.calls = 0

        async def stream_chat(self, messages, tools=None, metadata=None):
            del messages, tools, metadata
            yield StreamEvent(type=StreamEventType.DONE)

        async def simple_chat(self, messages, *, max_tokens=None) -> str:
            del messages, max_tokens
            self.calls += 1
            raise RuntimeError("HTTP 529 overloaded_error")

    async def unexpected_sleep(_delay: float) -> None:
        raise AssertionError("background 529 must not be retried")

    monkeypatch.setattr("backend.llm.base.asyncio.sleep", unexpected_sleep)
    adapter = _BusyAdapter()

    with pytest.raises(RuntimeError, match="529"):
        asyncio.run(
            adapter.side_query(
                [LLMMessage(role="user", content="page")],
                options=SideQueryOptions(operation="web_fetch_apply"),
            )
        )

    assert adapter.calls == 1


def test_side_query_retry_exhaustion_still_raises_without_raw_fallback(monkeypatch) -> None:
    class _AlwaysTransientAdapter(LLMAdapter):
        def __init__(self) -> None:
            self.calls = 0

        async def stream_chat(self, messages, tools=None, metadata=None):
            del messages, tools, metadata
            yield StreamEvent(type=StreamEventType.DONE)

        async def simple_chat(self, messages, *, max_tokens=None) -> str:
            del messages, max_tokens
            self.calls += 1
            raise ConnectionError("incomplete chunked read")

    async def fake_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr("backend.llm.base.asyncio.sleep", fake_sleep)
    monkeypatch.setattr("backend.llm.base.random.random", lambda: 0.0)
    adapter = _AlwaysTransientAdapter()

    with pytest.raises(ConnectionError, match="incomplete chunked read"):
        asyncio.run(
            adapter.side_query(
                [LLMMessage(role="user", content="raw page content must not escape")],
                options=SideQueryOptions(operation="web_fetch_apply"),
            )
        )

    # pi side-query contract: 3 retries after the initial attempt.
    assert adapter.calls == 4


def test_openai_side_query_uses_small_model_without_reasoning_or_cache() -> None:
    captured: dict = {}

    adapter = OpenAIAdapter(
        LLMSettings(
            api_key="test",
            provider="custom",
            model="main-reasoning-model",
            small_fast_model="small-fast-model",
            wire_api="responses",
            reasoning_effort="high",
            reasoning_effort_levels=("high",),
            responses_reasoning_summary="auto",
            prompt_cache_retention="24h",
        ),
    )
    async def fake_create(kwargs, **_options):
        captured.update(kwargs)
        return _openai_completed_stream()

    adapter._create_responses_request = fake_create  # type: ignore[method-assign]

    result = asyncio.run(
        adapter.side_query(
            [LLMMessage(role="user", content="page")],
            options=SideQueryOptions(
                operation="web_fetch_apply",
                max_tokens=4096,
                use_small_fast_model=True,
                disable_reasoning=True,
                enable_prompt_cache=False,
                output_schema={
                    "type": "object",
                    "properties": {"summary": {"type": "string"}},
                    "required": ["summary"],
                    "additionalProperties": False,
                },
            ),
        )
    )

    assert result == "condensed"
    assert captured["model"] == "small-fast-model"
    assert captured["max_output_tokens"] == 4096
    assert captured["store"] is False
    assert "reasoning" not in captured
    assert "prompt_cache_key" not in captured
    assert "prompt_cache_retention" not in captured
    assert captured["text"]["format"] == {
        "type": "json_schema",
        "name": "minicode_output_schema",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
            "additionalProperties": False,
        },
    }


def test_openai_side_query_disables_reasoning_only_when_wire_model_declares_none() -> None:
    captured: dict = {}

    adapter = OpenAIAdapter(
        LLMSettings(
            api_key="test",
            provider="openai",
            model="reasoning-model",
            small_fast_model="reasoning-model",
            wire_api="responses",
            reasoning_effort="high",
            reasoning_effort_levels=("none", "high"),
        ),
    )
    async def fake_create(kwargs, **_options):
        captured.update(kwargs)
        return _openai_completed_stream()

    adapter._create_responses_request = fake_create  # type: ignore[method-assign]

    result = asyncio.run(
        adapter.side_query(
            [LLMMessage(role="user", content="page")],
            options=SideQueryOptions(
                operation="web_fetch_apply",
                max_tokens=4096,
                use_small_fast_model=True,
                disable_reasoning=True,
                enable_prompt_cache=False,
                output_schema={
                    "type": "object",
                    "properties": {"summary": {"type": "string"}},
                    "required": ["summary"],
                    "additionalProperties": False,
                },
            ),
        )
    )

    assert result == "condensed"
    assert captured["model"] == "reasoning-model"
    assert captured["reasoning"] == {"effort": "none"}


def test_openai_side_query_does_not_inherit_primary_reasoning_config() -> None:
    captured: dict = {}

    adapter = OpenAIAdapter(
        LLMSettings(
            api_key="test",
            provider="custom",
            model="main-reasoning-model",
            small_fast_model="small-fast-model",
            wire_api="responses",
            reasoning_effort="high",
            reasoning_effort_levels=("high",),
            responses_reasoning_summary="auto",
        ),
    )
    async def fake_create(kwargs, **_options):
        captured.update(kwargs)
        return _openai_completed_stream()

    adapter._create_responses_request = fake_create  # type: ignore[method-assign]

    result = asyncio.run(
        adapter.side_query(
            [LLMMessage(role="user", content="page")],
            options=SideQueryOptions(
                operation="summary",
                max_tokens=1024,
                use_small_fast_model=True,
                disable_reasoning=False,
                enable_prompt_cache=False,
            ),
        )
    )

    assert result == "condensed"
    assert captured["model"] == "small-fast-model"
    assert "reasoning" not in captured


def test_anthropic_side_query_disables_cache_and_uses_small_model() -> None:
    captured: dict = {}
    adapter = AnthropicAdapter(
        api_key="test",
        model="claude-main",
        small_fast_model="claude-fast",
        max_tokens=8_000,
        thinking_budget=2_048,
    )

    adapter._http_client = _AnthropicClient(captured)
    result = asyncio.run(
        adapter.side_query(
            [
                LLMMessage(role="system", content="system"),
                LLMMessage(role="user", content="page"),
            ],
            options=SideQueryOptions(
                operation="web_fetch_apply",
                max_tokens=4096,
                use_small_fast_model=True,
                disable_reasoning=True,
                enable_prompt_cache=False,
                output_schema={
                    "type": "object",
                    "properties": {"summary": {"type": "string"}},
                    "required": ["summary"],
                    "additionalProperties": False,
                },
            ),
        )
    )

    assert result == "condensed"
    assert captured["model"] == "claude-fast"
    assert captured["max_tokens"] == 4096
    assert captured["system"] == "system"
    assert all("cache_control" not in message for message in captured["messages"])
    assert "thinking" not in captured
    assert captured["output_config"] == {
        "format": {
            "type": "json_schema",
            "schema": {
                "type": "object",
                "properties": {"summary": {"type": "string"}},
                "required": ["summary"],
                "additionalProperties": False,
            },
        }
    }
    assert captured["headers"]["anthropic-beta"] == "structured-outputs-2025-12-15"
