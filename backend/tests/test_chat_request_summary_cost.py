from __future__ import annotations

import json

import httpx
import pytest

from backend.config import LLMSettings
from backend.llm.base import StreamEventType
from backend.llm.openai_adapter import OpenAIAdapter


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
