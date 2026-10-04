from __future__ import annotations

import json

import pytest
from websockets.asyncio.server import serve
from websockets.datastructures import Headers
from websockets.http11 import Response

from backend.agent.provider_stream_error_event import provider_error_details
from backend.config import LLMSettings
from backend.llm.base import LLMMessage, StreamEventType
from backend.llm.openai_adapter import OpenAIAdapter


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,code,schema_type,provider_kind,error_kind,fatal,retryable",
    [
        (400, "context_length_exceeded", "invalid_request_error", "prompt_too_long", "prompt_too_long", False, True),
        (503, "model_not_found", "invalid_request_error", "model", "model", True, False),
        (429, "insufficient_quota", "billing_error", "billing", "billing", True, False),
        (403, "content_filter", "invalid_request_error", "content_filter", "blocked", True, False),
        (401, "invalid_api_key", "invalid_request_error", "auth", "auth", True, False),
    ],
)
async def test_rejected_real_websocket_handshake_keeps_provider_error_chain(
    status, code, schema_type, provider_kind, error_kind, fatal, retryable,
):
    secret = "sk-controlled-handshake-credential"
    body = json.dumps({"error": {
        "code": code, "type": schema_type,
        "message": f"Controlled rejection for {code}; API key: {secret}",
    }}).encode()
    handshakes = 0

    async def reject(connection, request):
        nonlocal handshakes
        handshakes += 1
        return Response(status, "Controlled rejection", Headers({
            "Content-Type": "application/json", "Content-Length": str(len(body)),
            "Retry-After": "11",
        }), body)

    async def handler(connection):
        raise AssertionError("Rejected handshake must not upgrade")

    async with serve(handler, "127.0.0.1", 0, process_request=reject) as server:
        port = server.sockets[0].getsockname()[1]
        adapter = OpenAIAdapter(LLMSettings(
            api_key="", model="audit-model", wire_api="responses",
            base_url=f"http://127.0.0.1:{port}/v1", proxy_mode="direct",
            responses_websocket=True,
        ))
        try:
            events = [event async for event in adapter.stream_chat(
                [LLMMessage(role="user", content="Controlled local audit")],
                metadata={"thread_id": "audit-thread", "run_id": "audit-run"},
            )]
        finally:
            await adapter.aclose()

    errors = [event for event in events if event.type is StreamEventType.ERROR]
    assert len(errors) == 1
    assert not any(event.type in {StreamEventType.DONE, StreamEventType.TOOL_CALL} for event in events)
    assert handshakes == 1
    error = errors[0]
    assert error.raw["status_code"] == status
    assert error.raw["provider_error_code"] == code
    assert error.raw["provider_error_schema_type"] == schema_type
    assert error.raw["retry_after_seconds"] == 11
    assert "Controlled rejection" in error.raw["provider_error_message"]
    assert secret not in json.dumps(error.raw) + error.content
    _, classification, failure_data = provider_error_details(error)
    assert classification.provider_error_type == provider_kind
    assert classification.error_type == error_kind
    assert classification.fatal is fatal
    assert classification.retryable is retryable
    assert failure_data["provider_error_code"] == code
