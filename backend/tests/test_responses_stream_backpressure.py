"""Responses streaming: bounded tool-argument deltas and retryable failed responses."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from backend.agent.policies.stream_retry import DefaultStreamRetryPolicy
from backend.agent.provider_stream_error_event import provider_error_details
from backend.config import AgentSettings, LLMSettings
from backend.llm.base import LLMMessage, StreamEvent, StreamEventType
from backend.llm.openai_adapter import OpenAIAdapter, _responses_error_event_raw


async def _collect(stream):
    return [event async for event in stream]


def test_tool_argument_deltas_are_debounced_and_final_arguments_are_exact():
    arguments = json.dumps({"file_path": "big.txt", "content": "x" * 4000})

    async def fake_stream():
        yield SimpleNamespace(type="response.output_item.added", output_index=0, item=SimpleNamespace(
            type="function_call", id="fc_1", call_id="call_1", name="write_file", arguments=""))
        for character in arguments:
            yield SimpleNamespace(type="response.function_call_arguments.delta", item_id="fc_1",
                call_id="call_1", output_index=0, delta=character)
        yield SimpleNamespace(type="response.function_call_arguments.done", item_id="fc_1", call_id="call_1",
            output_index=0, name="write_file", arguments=arguments)
        call = SimpleNamespace(type="function_call", id="fc_1", call_id="call_1", name="write_file",
            arguments=arguments, status="completed")
        yield SimpleNamespace(type="response.output_item.done", output_index=0, item=call)
        yield SimpleNamespace(type="response.completed", response=SimpleNamespace(id="r1", status="completed",
            usage=SimpleNamespace(input_tokens=1, output_tokens=1), output=[call]))

    adapter = OpenAIAdapter(LLMSettings(api_key="test", model="gpt-5.4", wire_api="responses"))

    async def fake_create(_kwargs, **_options):
        return fake_stream()

    adapter._create_responses_request = fake_create  # type: ignore[method-assign]
    events = asyncio.run(_collect(adapter.stream_chat([LLMMessage(role="user", content="hello")])))
    deltas = [event for event in events if event.type == StreamEventType.TOOL_CALL_DELTA]
    # One delta per character would re-parse the whole argument string 4000+ times downstream.
    assert 0 < len(deltas) <= len(arguments) // 128 + 2, len(deltas)
    assert deltas[0].tool_call_delta.partial_arguments == arguments[0]
    calls = [call for event in events if event.type == StreamEventType.TOOL_CALL for call in event.tool_calls]
    assert calls and all(call.arguments == json.loads(arguments) for call in calls)
    assert {call.id for call in calls} == {"call_1"}


@pytest.mark.parametrize(("error", "retry"), [
    ({"code": "server_error", "message": "x"}, True),
    ({"code": "processing_error", "message": "An error occurred while processing your request."}, True),
    ({"message": "Something went wrong."}, True),
    (SimpleNamespace(type="response.failed"), True),
    ({"code": "invalid_prompt", "message": "flagged"}, False),
    ({"code": "cyber_policy", "message": "blocked"}, False),
    ({"code": "insufficient_quota", "message": "You exceeded your current quota"}, False),
    ({"code": "invalid_api_key", "message": "Incorrect API key provided"}, False),
])
def test_failed_response_retries_unknown_codes_but_not_terminal_ones(error, retry):
    message, raw = _responses_error_event_raw("response.failed", error, fallback="Responses API response failed")
    event = StreamEvent(type=StreamEventType.ERROR, content=f"Responses API response failed: {message}", raw=raw)
    classification_input, _classification, _ = provider_error_details(event)
    assert DefaultStreamRetryPolicy(AgentSettings()).decide_retry(classification_input, 0).should_retry is retry
