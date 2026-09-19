"""Provider failures that must reach the retry ladder with the right shape."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from backend.agent.error_withholding import is_context_overflow_error
from backend.agent.policies.stream_retry import DefaultStreamRetryPolicy, StreamRetryState
from backend.agent.provider_stream_error_event import (
    handle_provider_error_event,
    provider_error_details,
)
from backend.agent.stream_attempt import StreamAttemptState, StreamTextState
from backend.config import AgentSettings, LLMSettings
from backend.llm.anthropic_adapter import AnthropicAdapter
from backend.llm.base import LLMMessage, UsageInfo, safe_stream_chat_with_request_metadata
from backend.llm.errors import retry_after_from_message
from backend.llm.openai_adapter import OpenAIAdapter


def _openai(wire: str, handler) -> tuple[OpenAIAdapter, httpx.AsyncClient]:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = OpenAIAdapter(
        LLMSettings(api_key="k", model="m", base_url="https://fixture.invalid/v1", wire_api=wire),
        http_client=client,
    )
    return adapter, client


async def _first_error(adapter, *, wrapped: bool = False):
    stream = (
        safe_stream_chat_with_request_metadata(adapter, [LLMMessage("user", "hi")])
        if wrapped
        else adapter.stream_chat([LLMMessage("user", "hi")])
    )
    events = [event async for event in stream]
    return next(event for event in events if event.type.name == "ERROR")


async def _drive(event, *, retry_state: StreamRetryState, attempt: int, waits: list[float], max_attempts: int = 3):
    settings = AgentSettings(stream_max_attempts=max_attempts, stream_retry_delay_seconds=0.5)

    async def noop(*args, **kwargs):
        return None

    async def never(**kwargs):
        return False

    async def degrade(**kwargs):
        yield f"TERMINATED({kwargs['profile'].failed_stopped_reason})"

    result = None
    async for item in handle_provider_error_event(
        event,
        state=SimpleNamespace(),
        context_builder=None,
        turn_kernel=SimpleNamespace(close_provider_attempt=noop, emit_runtime_span=None),
        provider_attempt=SimpleNamespace(span_id="s"),
        stream_state=StreamAttemptState(),
        stream_text=StreamTextState(),
        pending_tool_calls=[],
        usage=UsageInfo(),
        turn_usage=UsageInfo(),
        stream_retry_policy=DefaultStreamRetryPolicy(settings),
        stream_attempt=attempt,
        stream_recovery_attempted=False,
        budget_runtime=SimpleNamespace(
            bounded_provider_timeout=lambda delay: (waits.append(delay) or 0.0, False),
            consume_retry=lambda reason: None,
        ),
        error_controller=None,
        iteration_id_value="i",
        cancel_event=None,
        degrade_and_finish=degrade,
        recover_withheld_error=never,
        max_retries=max_attempts,
        query_source="user",
        retry_state=retry_state,
    ):
        result = item
    return result


def test_connect_failure_from_a_real_adapter_takes_the_connect_track() -> None:
    async def scenario():
        async def handler(request):
            raise httpx.ConnectError("[Errno 11001] getaddrinfo failed", request=request)

        adapter, client = _openai("chat", handler)
        async with client:
            event = await _first_error(adapter, wrapped=True)
        assert event.raw.get("connect_phase") is True

        state = StreamRetryState()
        waits: list[float] = []
        attempt = 0
        for _ in range(4):
            result = await _drive(event, retry_state=state, attempt=attempt, waits=waits)
            attempt = result.stream_attempt
            assert result.action == "retry"
        # The request budget (3) is never spent while the provider is unreachable.
        assert attempt == 0
        assert state.connection_retries == 4
        assert waits == [5.0, 10.0, 20.0, 40.0]

        # The same failure without the connect-phase verdict spends the budget.
        event.raw.pop("connect_phase")
        state = StreamRetryState()
        attempt = 0
        actions = []
        for _ in range(4):
            result = await _drive(event, retry_state=state, attempt=attempt, waits=[])
            attempt = result.stream_attempt
            actions.append(result.action)
        assert actions == ["retry", "retry", "retry", "finish"]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("wire", "body"),
    [
        (
            "responses",
            "event: response.failed\ndata: "
            + json.dumps({"type": "response.failed", "sequence_number": 3, "response": {"id": "r", "status": "failed", "error": {"code": "server_error", "message": "The server had an error while processing your request."}}})
            + "\n\n",
        ),
        (
            "chat",
            "data: " + json.dumps({"error": {"message": "The server had an error while processing your request.", "type": "server_error", "code": None}}) + "\n\n",
        ),
    ],
)
def test_in_stream_server_error_is_retried(wire: str, body: str) -> None:
    async def scenario():
        async def handler(request):
            return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

        adapter, client = _openai(wire, handler)
        async with client:
            event = await _first_error(adapter)
        _, classification, _ = provider_error_details(event)
        assert classification.retryable and not classification.fatal
        assert classification.provider_error_type == "network"
        result = await _drive(event, retry_state=StreamRetryState(), attempt=0, waits=[])
        assert result.action == "retry" and result.stream_attempt == 1

    asyncio.run(scenario())


def test_rate_limit_delay_stated_in_the_message_is_honoured() -> None:
    message = "Rate limit reached on tokens per min (TPM): Limit 30000. Please try again in 11.054s."
    assert retry_after_from_message(message) == pytest.approx(11.054)
    assert retry_after_from_message("try again in 500ms") == pytest.approx(0.5)
    assert retry_after_from_message("try again in 2 seconds") == pytest.approx(2.0)
    assert retry_after_from_message("no delay here") == 0.0

    async def scenario():
        failed = {"type": "response.failed", "sequence_number": 3, "response": {"id": "r", "status": "failed", "error": {"code": "rate_limit_exceeded", "message": message}}}
        http429 = {"error": {"message": message, "type": "tokens", "code": "rate_limit_exceeded"}}
        for handler in (
            lambda request: httpx.Response(429, content=json.dumps(http429), headers={"content-type": "application/json"}),
            lambda request: httpx.Response(200, content="event: response.failed\ndata: " + json.dumps(failed) + "\n\n", headers={"content-type": "text/event-stream"}),
        ):
            adapter, client = _openai("responses", handler)
            async with client:
                event = await _first_error(adapter)
            assert event.raw.get("retry_after_seconds") == pytest.approx(11.054)
            waits: list[float] = []
            result = await _drive(event, retry_state=StreamRetryState(), attempt=0, waits=waits)
            assert result.action == "retry"
            assert waits == [pytest.approx(11.054)]

    asyncio.run(scenario())


def test_anthropic_max_tokens_context_limit_is_a_context_overflow() -> None:
    body = {
        "type": "error",
        "error": {
            "type": "invalid_request_error",
            "message": "input length and `max_tokens` exceed context limit: 188059 + 20000 > 200000",
        },
    }

    async def scenario():
        async def handler(request):
            return httpx.Response(400, content=json.dumps(body), headers={"content-type": "application/json"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = AnthropicAdapter(api_key="k", model="claude-x", base_url="https://fixture.invalid")
            adapter._http_client = client
            event = await _first_error(adapter)
        classification_input, classification, _ = provider_error_details(event)
        assert classification.error_type == "prompt_too_long"
        assert is_context_overflow_error(classification_input)

    asyncio.run(scenario())
