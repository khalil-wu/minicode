"""Provider envelope boundaries.

Cumulative Anthropic message_delta events, wrapped Responses websocket error
envelopes, websocket proxy resolution and model-metadata retention across a
discovery refresh; each test drives the production entry the read-only audit
reproduced with its probes.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

from backend.agent.provider_stream_error_event import provider_error_details
from backend.api.models import LLMSettingsUpdateRequest
from backend.config import LLMSettings
from backend.llm.anthropic_adapter import AnthropicAdapter
from backend.llm.base import LLMMessage
from backend.llm.openai_adapter import OpenAIAdapter
from backend.llm.proxy_policy import provider_proxy_url_for_base_url
from backend.services import llm_provider_helpers, llm_provider_service


# ---------------------------------------------------------------- L01

def _anthropic_sse(extra_delta: bool) -> bytes:
    events = [
        {"type": "message_start", "message": {"id": "audit-msg", "type": "message", "role": "assistant",
            "content": [], "model": "audit-model", "stop_reason": None,
            "usage": {"input_tokens": 3, "output_tokens": 0}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "audit answer"}},
        {"type": "content_block_stop", "index": 0},
        *([{"type": "message_delta", "delta": {"stop_reason": None, "stop_sequence": None},
            "usage": {"output_tokens": 1}}] if extra_delta else []),
        {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
         "usage": {"output_tokens": 5}},
        {"type": "message_stop"},
    ]
    return "".join("event: " + item["type"] + "\ndata: " + json.dumps(item) + "\n\n"
                   for item in events).encode("utf-8")


@pytest.mark.asyncio
@pytest.mark.parametrize("extra_delta", [False, True])
async def test_anthropic_accepts_cumulative_message_deltas(extra_delta):
    body = _anthropic_sse(extra_delta)

    async def mock(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    adapter = AnthropicAdapter(api_key="audit-placeholder", model="audit-model", base_url="https://synthetic.invalid/v1")
    adapter._http_client = httpx.AsyncClient(transport=httpx.MockTransport(mock))
    try:
        observed = []
        async for item in adapter.stream_chat([LLMMessage(role="user", content="audit prompt")]):
            observed.append(item)
    finally:
        await adapter.aclose()
    assert not [item for item in observed if item.type.value == "error"]
    done = [item for item in observed if item.type.value == "done"]
    assert done and done[-1].finish_reason == "end_turn"
    assert done[-1].usage is not None and done[-1].usage.output_tokens == 5
    assert any("audit answer" in (item.content or "") for item in observed)


# ---------------------------------------------------------------- L02

class _Policy:
    managed = False

    def acquire(self, _url):
        return SimpleNamespace(check=lambda: None)


class _Socket:
    def __init__(self, event):
        from websockets.protocol import State

        self.state = State.OPEN
        self.response = SimpleNamespace(status_code=101, headers={})
        self.event = event

    async def send(self, _payload):
        pass

    async def recv(self):
        return json.dumps(self.event)

    async def close(self):
        from websockets.protocol import State

        self.state = State.CLOSED


async def _observe_envelope(envelope):
    async def connector(_url, _connect, **_kwargs):
        return _Socket(envelope)

    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    adapter = OpenAIAdapter(LLMSettings(provider="custom", api_key="audit-placeholder",
        base_url="https://synthetic.invalid/v1", model="audit-model", wire_api="responses",
        responses_websocket=True), http_client=client, network_policy=_Policy())
    try:
        with patch("backend.application_network.application_websocket_connect", connector):
            observed = []
            async for event in adapter.stream_chat([LLMMessage(role="user", content="audit prompt")],
                    metadata={"conversation_id": "audit-thread", "run_id": "audit-run"}):
                _, classification, _ = provider_error_details(event)
                observed.append({"type": event.type.value,
                    "status_code": event.raw.get("status_code"),
                    "retry_after_seconds": event.raw.get("retry_after_seconds"),
                    "provider_error_type": classification.provider_error_type,
                    "retryable": classification.retryable})
        return observed
    finally:
        await adapter.aclose()
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("envelope,status,delay", [
    ({"type": "error", "status": 429, "error": {"code": "rate_limit_exceeded", "type": "rate_limit_error",
                                                "message": "Audit capacity reached"}, "headers": {"Retry-After": "9"}},
     429, 9.0),
    ({"type": "error", "status": 503, "error": {"message": "Audit please try later"},
      "headers": {"Retry-After": "3"}}, 503, 3.0),
    ({"type": "error", "error": {"status": 503, "message": "Audit please try later"}}, 503, None),
])
async def test_wrapped_websocket_errors_keep_outer_status_and_retry_after(envelope, status, delay):
    observed = await _observe_envelope(envelope)
    error = observed[-1]
    assert error["type"] == "error"
    assert error["status_code"] == status
    assert error["retry_after_seconds"] == delay
    if status == 429:
        assert error["provider_error_type"] == "rate_limit" and error["retryable"] is True


# ---------------------------------------------------------------- N03

def test_websocket_urls_use_https_proxy_rules_and_default_ports(monkeypatch):
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy",
                 "NO_PROXY", "no_proxy", "LLM_PROXY_URL", "MINICODE_LLM_PROXY_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://audit-proxy.invalid:8443")
    assert provider_proxy_url_for_base_url("wss://audit-api.invalid/v1") == "http://audit-proxy.invalid:8443"
    monkeypatch.setenv("HTTP_PROXY", "http://audit-http-proxy.invalid:8080")
    monkeypatch.setenv("NO_PROXY", "audit-api.invalid:443")
    assert provider_proxy_url_for_base_url("wss://audit-api.invalid/v1") == ""
    assert provider_proxy_url_for_base_url("https://audit-api.invalid/v1") == ""
    monkeypatch.setenv("NO_PROXY", "audit-api.invalid:80")
    assert provider_proxy_url_for_base_url("ws://audit-api.invalid/v1") == ""
    monkeypatch.delenv("NO_PROXY")
    monkeypatch.delenv("HTTPS_PROXY")
    assert provider_proxy_url_for_base_url("ws://audit-api.invalid/v1") == "http://audit-http-proxy.invalid:8080"


# ---------------------------------------------------------------- A06

def test_live_model_refresh_preserves_the_configured_tool_mode(monkeypatch):
    current = {
        "base_url": "https://saved.example/v1", "model": "selected", "wire_api": "responses",
        "api_key": "fixture",
        "model_metadata": {"selected": {"tool_mode": "code_mode", "supports_custom_tools": True}},
    }
    monkeypatch.setattr(llm_provider_service, "get_openai_settings", lambda: current)
    request = LLMSettingsUpdateRequest.model_validate({"provider": "openai",
        "openai": {"api_key": "fixture", "wire_api": "responses", "base_url": current["base_url"]}})

    async def discover(*args, **kwargs):
        return llm_provider_helpers.ModelDiscovery(["selected"], {"selected": {"context_window": 200_000}})

    result = asyncio.run(llm_provider_service.refresh_llm_models(request, fetch_openai_models=discover))
    metadata = result["model_metadata"]["selected"]
    assert metadata["tool_mode"] == "code_mode"
    assert metadata["context_window"] == 200_000
    assert metadata["supports_custom_tools"] is True
