"""Provider item boundaries on the real HTTP and WebSocket request paths."""
from copy import deepcopy
from dataclasses import replace
import json

import httpx
import pytest

from backend.config import LLMSettings
from backend.llm.base import LLMMessage, StreamEventType
from backend.llm.openai_adapter import OpenAIAdapter
from backend.tests.test_responses_websocket import collect, endpoint, terminal


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["custom", "openai"])
async def test_http_cleans_only_internal_item_fields_and_keeps_encrypted_context(provider):
    metadata = {"turn_id": "turn-original", "content_item_kinds": ["user.text"]}
    items = [
        {"type": "message", "role": "user", "content": "hello", "encrypted_content": "message-token",
            "internal_chat_message_metadata_passthrough": metadata},
        {"type": "function_call", "call_id": "call-original", "name": "inspect", "arguments": "{}",
            "encrypted_content": "call-token", "encrypted_function_args": ["opaque"],
            "internal_chat_message_metadata_passthrough": metadata},
        {"type": "function_call_output", "call_id": "call-original", "output": json.dumps({
            "encrypted_content": "user-owned result", "internal_chat_message_metadata_passthrough": "user data"})},
        *[{"type": kind, "encrypted_content": kind + "-token"} for kind in ("reasoning", "compaction", "context_compaction")],
        {"type": "agent_message", "author": "child", "recipient": "root", "content": [
            {"type": "encrypted_content", "encrypted_content": "attributed-agent-payload"}]},
    ]
    original = deepcopy(items)
    captured = []
    async def respond(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json=terminal("response")["response"])
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = OpenAIAdapter(LLMSettings(provider=provider, api_key="fixture", model="fixture-model",
            wire_api="responses", base_url="https://example.test/v1"), http_client=client)
        try:
            await adapter._create_responses_request({"model": "fixture-model", "input": items})
        finally:
            await adapter.aclose()
    sent = captured[0]["input"]
    assert items == original
    assert sent[2:] == original[2:]
    if provider == "openai":
        assert sent == original
    else:
        assert "internal_chat_message_metadata_passthrough" not in sent[0]
        assert "internal_chat_message_metadata_passthrough" not in sent[1]
        assert "encrypted_content" not in sent[0] and "encrypted_content" not in sent[1]
        assert "encrypted_function_args" not in sent[1]
        assert sent[0]["content"] == "hello" and sent[1]["arguments"] == "{}"


@pytest.mark.asyncio
async def test_custom_websocket_omits_admission_metadata_without_mutating_host_identity():
    async def respond(socket, request, index):
        await socket.send(json.dumps(terminal(str(index))))
    message = LLMMessage(role="user", content="hello", is_user_input=True,
        accepted_input={"order": 1, "model_item_id": "msg_original", "turn_id": "original",
            "single_text": True, "text_start": 0, "text_length": 5})
    facts = deepcopy(message.accepted_input)
    async with endpoint(respond) as (adapter, requests, _connections):
        adapter._settings = replace(adapter._settings, provider="custom")
        events = await collect(adapter, [message])
        assert events[-1].type == StreamEventType.DONE
        admitted = next(item for item in requests[0]["input"] if item.get("id") == "msg_original")
        assert "internal_chat_message_metadata_passthrough" not in admitted
        assert admitted["content"] == [{"type": "input_text", "text": "hello"}]
    assert message.accepted_input == facts
