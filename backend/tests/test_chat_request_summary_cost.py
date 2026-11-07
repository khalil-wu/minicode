from __future__ import annotations

import json

import httpx
import pytest

from backend.config import LLMSettings
from backend.llm.base import LLMMessage, StreamEventType
from backend.llm.openai_adapter import OpenAIAdapter


@pytest.mark.asyncio
async def test_first_text_is_delivered_before_full_request_diagnostics(monkeypatch):
    from backend.llm import openai_adapter

    summarized = []
    original = openai_adapter._safe_request_summary
    def summarize(**kwargs):
        summarized.append(kwargs["request_params"])
        return original(**kwargs)
    monkeypatch.setattr(openai_adapter, "_safe_request_summary", summarize)
    def reply(request):
        assert not summarized
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
            text='data: {"choices":[{"delta":{"content":"first text"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')
    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        adapter = OpenAIAdapter(LLMSettings(api_key="fixture", model="fixture", wire_api="chat",
            base_url="https://fixture.invalid/v1", proxy_mode="direct"), http_client=client)
        try:
            stream = adapter._stream_chat_completions_http({"model": "fixture", "stream": True,
                "messages": [{"role": "user", "content": f"retained {index}"} for index in range(1000)]})
            first = await anext(stream)
            assert first.type == StreamEventType.TEXT_CHUNK and first.content == "first text"
            assert not summarized
            events = [event async for event in stream]
            assert events[-1].raw["request_summary"]["input_items_len"] == 1000
            assert len(summarized) == 1
        finally:
            await adapter.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("wire", ["chat", "responses", "anthropic"])
async def test_streamed_text_and_usage_precede_diagnostics_with_actual_hook_model(monkeypatch, wire):
    from backend.llm import openai_adapter, anthropic_adapter
    from backend.tests.test_provider_envelope_boundaries import _anthropic_sse
    from backend.tests.test_responses_websocket import terminal

    module = anthropic_adapter if wire == "anthropic" else openai_adapter
    helper = "_anthropic_safe_request_summary_from_payload" if wire == "anthropic" else "_safe_request_summary"
    summarized = []
    original = getattr(module, helper)

    def summarize(*args, **kwargs):
        result = original(*args, **kwargs)
        summarized.append(result)
        return result

    async def hook(_metadata, payload):
        return {**payload, "model": "actual-hook-model"}

    monkeypatch.setattr(module, helper, summarize)
    monkeypatch.setattr(module, "emit_provider_lifecycle_request", hook)
    if wire == "anthropic":
        body = _anthropic_sse(False).decode().replace("audit answer", "first text")
    elif wire == "responses":
        events = [
            {"type": "response.output_text.delta", "item_id": "msg-answer", "output_index": 0,
             "content_index": 0, "delta": "first text"},
            terminal("answer", text="first text"),
        ]
        body = "".join("data: " + json.dumps(event) + "\n\n" for event in events)
    else:
        body = 'data: {"choices":[{"delta":{"content":"first text"},"finish_reason":null}]}\n\ndata: {"choices":[{"delta":{"content":" second text"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'

    def reply(request):
        assert not summarized
        assert json.loads(request.content)["model"] == "actual-hook-model"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        if wire == "anthropic":
            adapter = anthropic_adapter.AnthropicAdapter("fixture", model="fixture", base_url="https://fixture.invalid/v1")
            adapter._http_client = client
        else:
            adapter = OpenAIAdapter(LLMSettings(api_key="fixture", model="fixture", wire_api=wire,
                base_url="https://fixture.invalid/v1", proxy_mode="direct"), http_client=client)
        try:
            observed = []
            async for event in adapter.stream_chat([LLMMessage(role="user", content="respond")]):
                observed.append(event)
                if event.type in {StreamEventType.TEXT_CHUNK, StreamEventType.USAGE}:
                    assert not summarized
                if event.type == StreamEventType.USAGE:
                    assert event.raw["model"] == "actual-hook-model"
            assert observed[-1].type == StreamEventType.DONE
            assert "first text" in "".join(event.content or "" for event in observed if event.type == StreamEventType.TEXT_CHUNK)
            assert len(summarized) == 1
            assert observed[-1].raw["request_summary"]["model"] == "actual-hook-model"
        finally:
            await adapter.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("retry", [False, True])
async def test_each_wire_attempt_summarizes_only_its_final_payload(monkeypatch, retry):
    from backend.llm import openai_adapter

    summaries, sent = [], []
    summarize = openai_adapter._safe_request_summary
    def observe(**kwargs):
        summaries.append(kwargs)
        return summarize(**kwargs)
    monkeypatch.setattr(openai_adapter, "_safe_request_summary", observe)
    async def hook(_metadata, payload):
        return {**payload, "model": "hook-model", "messages": [*payload["messages"], {"role": "developer", "content": "hook context"}]}
    monkeypatch.setattr(openai_adapter, "emit_provider_lifecycle_request", hook)
    def reply(request):
        sent.append(json.loads(request.content))
        if retry and len(sent) == 1:
            return httpx.Response(400, json={"error": {"message": "Unrecognized request argument supplied: store", "param": "store"}})
        chunk = {"choices": [{"delta": {"content": "done"}, "finish_reason": "stop"}]}
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
            text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n")
    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        adapter = OpenAIAdapter(LLMSettings(api_key="fixture", model="fixture", wire_api="chat",
            base_url="https://fixture.invalid/v1", proxy_mode="direct"), http_client=client)
        try:
            events = [event async for event in adapter._stream_chat_completions_http({
                "model": "fixture", "messages": [{"role": "user", "content": "input"}], "stream": True, "store": False})]
        finally:
            await adapter.aclose()
    assert events[-1].type == StreamEventType.DONE
    assert len(summaries) == len(sent) == (2 if retry else 1)
    assert all(summary["model"] == "hook-model" for summary in summaries)
    assert all(summary["request_params"] == payload for summary, payload in zip(summaries, sent))
    assert events[-1].raw["request_summary"]["model"] == "hook-model"
    if retry:
        assert "store" not in sent[-1]
