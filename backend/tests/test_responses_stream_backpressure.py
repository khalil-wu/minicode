"""Responses streaming: retryable failed responses."""
from types import SimpleNamespace

import pytest

from backend.agent.policies.stream_retry import DefaultStreamRetryPolicy
from backend.agent.provider_stream_error_event import provider_error_details
from backend.config import AgentSettings
from backend.llm.base import StreamEvent, StreamEventType
from backend.llm.openai_adapter import _responses_error_event_raw


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
