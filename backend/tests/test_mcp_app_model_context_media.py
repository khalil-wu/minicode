from __future__ import annotations

import base64
from copy import deepcopy
from io import BytesIO
import json
from types import SimpleNamespace

from PIL import Image
import pytest

from backend.agent.context import ContextBuilder
from backend.agent.state import AgentState
from backend.config import LLMSettings
from backend.llm.anthropic_adapter import AnthropicAdapter
from backend.llm.base import StreamEventType
from backend.llm.capabilities import ProviderCapabilities
from backend.llm.openai_adapter import OpenAIAdapter
from backend.tests.guardian_http_fixture import classifier_events, http_endpoint, send_sse
from backend.tests.test_mcp_apps_chain import owned_invocation


def png(red):
    buffer = BytesIO()
    Image.new("RGB", (2, 2), (red, 20, 60)).save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def pdf():
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Contents 4 0 R >>", b"<< /Length 0 >>\nstream\n\nendstream"]
    body, offsets = b"%PDF-1.4\n", []
    for index, value in enumerate(objects, 1):
        offsets.append(len(body))
        body += str(index).encode() + b" 0 obj\n" + value + b"\nendobj\n"
    xref = len(body)
    body += b"xref\n0 5\n0000000000 65535 f \n" + b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    body += f"trailer\n<< /Size 5 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return base64.b64encode(body).decode("ascii")


def response_handler(wire):
    async def respond(call, reader, writer):
        if wire == "responses":
            await send_sse(writer, classifier_events(call.index, final="Observed App context."))
        elif wire == "chat":
            await send_sse(writer, [
                {"id": "app-context", "choices": [{"index": 0, "delta": {"content": "Observed App context."}, "finish_reason": None}]},
                {"id": "app-context", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            ])
        else:
            await send_sse(writer, [
                {"type": "message_start", "message": {"id": "app-context", "type": "message", "role": "assistant", "model": "fixture", "content": [], "usage": {"input_tokens": 10, "output_tokens": 0}}},
                {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Observed App context."}},
                {"type": "content_block_stop", "index": 0},
                {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 5}},
                {"type": "message_stop"},
            ])
    return respond


def adapter_for(wire, url):
    if wire == "anthropic":
        return AnthropicAdapter("controlled-local-key", model="fixture", base_url=url + "/v1", proxy_mode="direct")
    return OpenAIAdapter(LLMSettings(provider="custom", model="fixture", api_key="controlled-local-key",
        base_url=url + "/v1", wire_api=wire, proxy_mode="direct"))


def user_parts(body, wire):
    parts = []
    for item in body["input" if wire == "responses" else "messages"]:
        if item.get("role") == "user":
            content = item["content"]
            parts.extend(content if isinstance(content, list) else [{"type": "text", "text": content}])
    return parts


@pytest.mark.asyncio
@pytest.mark.parametrize("wire", ["responses", "chat", "anthropic"])
async def test_real_app_context_pixels_resource_order_and_latest_update_reach_provider_http(tmp_path, wire, request):
    first, second = png(10), png(20)
    payload = {"content": [{"type": "text", "text": "APP_TEXT_BEFORE"},
        {"type": "image", "data": first, "mimeType": "image/png", "_meta": {"codex/imageDetail": "original"}},
        {"type": "text", "text": "APP_TEXT_BETWEEN"},
        {"type": "resource", "resource": {"uri": "mcp://owned/picture.png", "mimeType": "image/png", "blob": second}},
        {"type": "resource", "resource": {"uri": "mcp://owned/note.txt", "mimeType": "text/plain", "text": "APP_RESOURCE_TEXT"}},
        {"type": "resource_link", "uri": "mcp://owned/later", "name": "APP_RESOURCE_LINK"},
        {"type": "text", "text": "APP_TEXT_AFTER </mcp_app_context>"}], "structuredContent": {"selection": 9}}
    manager, runtime, context, result = await owned_invocation(tmp_path)
    receipt = result.runtime_metadata["mcp_app_receipt"]
    try:
        view = await runtime.open(receipt.invocation_id, "conversation")
        await runtime.request(receipt.invocation_id, "conversation", view["viewId"], "ui/update-model-context", payload)
        original = deepcopy(runtime.model_context("owned-store", "conversation"))
        async with http_endpoint(response_handler(wire)) as endpoint:
            adapter = adapter_for(wire, endpoint.url)
            try:
                builder = ContextBuilder(llm=adapter, conversation_id="conversation", workspace_root=tmp_path)
                builder.bind_tool_context(context)
                builder.append_user("Inspect the App selection")
                before = deepcopy(builder.guardian_evidence.snapshot())
                state = AgentState(user_message="Inspect the App selection", conversation_id="conversation", workspace_root=tmp_path)
                messages = await builder.build(state)
                assert sum(len(message.images) for message in messages) == 2
                assert not runtime.model_context_messages("another-store", "conversation", llm=adapter)
                assert runtime.model_context("owned-store", "conversation") == original
                events = [event async for event in adapter.stream_chat(messages)]
                assert any(event.type == StreamEventType.DONE for event in events)
                assert not any(event.type == StreamEventType.ERROR for event in events), events
                body = endpoint.requests[-1].body
                parts = user_parts(body, wire)
                image_indexes = [index for index, part in enumerate(parts) if part["type"] in {"input_image", "image_url", "image"}]
                assert len(image_indexes) == 2
                assert "APP_TEXT_BEFORE" in parts[image_indexes[0] - 1]["text"]
                assert "APP_TEXT_BETWEEN" in parts[image_indexes[1] - 1]["text"]
                assert "APP_RESOURCE_TEXT" in parts[image_indexes[1] + 1]["text"]
                text = "\n".join(part.get("text", "") for part in parts)
                assert first not in text and second not in text and "UI_ONLY_SECRET" not in text
                assert "APP_RESOURCE_LINK" in text and '"selection": 9' in text
                assert "\\u003c/mcp_app_context>" in text
                assert json.dumps(body).count(first) == json.dumps(body).count(second) == 1
                if wire == "responses":
                    assert parts[image_indexes[0]]["detail"] == "original"
                elif wire == "chat":
                    assert parts[image_indexes[0]]["image_url"]["detail"] == "original"
                assert not any(message.images or "APP_TEXT_BEFORE" in message.content for message in builder._history)
                assert builder.guardian_evidence.snapshot().transcript == before.transcript
                await runtime.request(receipt.invocation_id, "conversation", view["viewId"], "ui/update-model-context", {"content": [{"type": "text", "text": "ONLY_LATEST_CONTEXT"}]})
                latest = await builder.build(state)
                events = [event async for event in adapter.stream_chat(latest)]
                assert not any(event.type == StreamEventType.ERROR for event in events), events
                assert len(endpoint.requests) == 2
                latest_body = json.dumps(endpoint.requests[-1].body)
                assert "ONLY_LATEST_CONTEXT" in latest_body and first not in latest_body and "APP_TEXT_BEFORE" not in latest_body
                request.node.user_properties.append(("actual_mcp_app_context_http", json.dumps({"api": wire,
                    "requests": 2, "native_images": 2, "original_detail": wire != "anthropic", "resource_text": True,
                    "resource_link": True, "latest_update_replaces_pixels": True, "history_and_guardian_unchanged": True})))
            finally:
                await adapter.aclose()
    finally:
        await manager.stop_all()
        await runtime.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("wire", ["responses", "anthropic"])
async def test_embedded_pdf_resource_reaches_native_provider_document(tmp_path, wire):
    data = pdf()
    manager, runtime, context, result = await owned_invocation(tmp_path)
    receipt = result.runtime_metadata["mcp_app_receipt"]
    try:
        view = await runtime.open(receipt.invocation_id, "conversation")
        await runtime.request(receipt.invocation_id, "conversation", view["viewId"], "ui/update-model-context", {
            "content": [{"type": "resource", "resource": {"uri": "mcp://owned/app.pdf", "mimeType": "application/pdf", "blob": data}}]})
        async with http_endpoint(response_handler(wire)) as endpoint:
            adapter = adapter_for(wire, endpoint.url)
            try:
                builder = ContextBuilder(llm=adapter, conversation_id="conversation", workspace_root=tmp_path)
                builder.bind_tool_context(context)
                messages = await builder.build(AgentState(user_message="Read the App PDF", conversation_id="conversation", workspace_root=tmp_path))
                assert sum(len(message.documents) for message in messages) == 1
                events = [event async for event in adapter.stream_chat(messages)]
                assert not any(event.type == StreamEventType.ERROR for event in events), events
                parts = user_parts(endpoint.requests[0].body, wire)
                documents = [part for part in parts if part["type"] in {"input_file", "document"}]
                assert len(documents) == 1
                assert data not in "\n".join(part.get("text", "") for part in parts)
                assert json.dumps(documents).count(data) == 1
            finally:
                await adapter.aclose()
    finally:
        await manager.stop_all()
        await runtime.aclose()


@pytest.mark.asyncio
async def test_text_only_projection_retains_original_app_pixels_for_next_model(tmp_path):
    manager, runtime, _, result = await owned_invocation(tmp_path)
    receipt = result.runtime_metadata["mcp_app_receipt"]
    data = png(30)
    try:
        view = await runtime.open(receipt.invocation_id, "conversation")
        payload = {"content": [{"type": "image", "mimeType": "image/png", "data": data}]}
        await runtime.request(receipt.invocation_id, "conversation", view["viewId"], "ui/update-model-context", payload)
        text_only = SimpleNamespace(capabilities=ProviderCapabilities(wire_api="chat", vision=False))
        hidden = runtime.model_context_messages("owned-store", "conversation", llm=text_only)
        assert not any(message.images for message in hidden)
        assert "pixels were not sent" in "\n".join(message.content for message in hidden)
        visible = runtime.model_context_messages("owned-store", "conversation", llm=None)
        assert [image["data"] for message in visible for image in message.images] == [data]
        assert runtime.model_context("owned-store", "conversation")[0]["context"] == payload
    finally:
        await manager.stop_all()
        await runtime.aclose()


@pytest.mark.asyncio
async def test_one_update_shares_existing_media_count_budget_without_erasing_original_blocks(tmp_path):
    manager, runtime, _, result = await owned_invocation(tmp_path)
    receipt = result.runtime_metadata["mcp_app_receipt"]
    data = png(40)
    try:
        view = await runtime.open(receipt.invocation_id, "conversation")
        payload = {"content": [{"type": "image", "mimeType": "image/png", "data": data} for _ in range(101)]}
        await runtime.request(receipt.invocation_id, "conversation", view["viewId"], "ui/update-model-context", payload)
        messages = runtime.model_context_messages("owned-store", "conversation", llm=None)
        assert sum(len(message.images) for message in messages) == 100
        assert "provider maximum of 100 media items" in "\n".join(message.content for message in messages)
        assert len(runtime.model_context("owned-store", "conversation")[0]["context"]["content"]) == 101
        assert all(data not in message.content for message in messages)
    finally:
        await manager.stop_all()
        await runtime.aclose()
