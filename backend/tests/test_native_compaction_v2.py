from __future__ import annotations

import asyncio
from copy import deepcopy
import json

import httpx
import pytest

from backend.agent.context import ContextBuilder
from backend.agent.state import AgentState
from backend.config import AppConfig, LLMSettings
from backend.config_layers import ConfigLayer, ConfigLayerSource, ConfigLayerStack
from backend.conversations.repository import ConversationRepository
from backend.llm.base import LLMMessage, LLMTurnContext
from backend.llm.compaction_v2 import retained_input
from backend.llm.native_compaction import native_compaction_windows
from backend.llm.openai_adapter import OpenAIAdapter, ResponsesStreamError
from backend.llm.responses_input import agent_message_response_item
from backend.services.llm_adapter_factory import create_session_llm


def settings(**kwargs):
    return LLMSettings(provider="custom", model="fixture-v2", base_url="https://fixture.invalid/v1",
        api_key="fixture", wire_api="responses", max_tokens=1024, **kwargs)


def events(*, count=1, terminal="response.completed"):
    values = [{"type": "response.output_item.done", "item": {"type": "compaction", "id": f"cmp-{index}",
        "encrypted_content": f"opaque-{index}"}} for index in range(count)]
    if terminal:
        values.append({"type": terminal, "response": {"id": "compact-response", "status": "completed",
            "usage": {"input_tokens": 120, "output_tokens": 15},
            "output": [{"type": "compaction", "id": "cmp-0", "encrypted_content": "opaque-0"}]}})
    return values


def response(values):
    return httpx.Response(200, headers={"content-type": "text/event-stream"},
        content="".join("data: " + json.dumps(value) + "\n\n" for value in values).encode())


@pytest.mark.asyncio
async def test_v2_trigger_retains_admitted_inputs_and_recovers_exact_wire(tmp_path):
    requests = []

    def send(request):
        requests.append((str(request.url), json.loads(request.content)))
        return response(events())

    async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as client:
        adapter = OpenAIAdapter(settings(), http_client=client)
        adapter.configure_remote_compaction("v2")
        builder = ContextBuilder(llm=adapter)
        turn = LLMTurnContext()
        builder.bind_llm_turn_context(turn)
        await builder.start_turn("Keep the original task", AgentState(user_message="Keep the original task"))
        builder.append_user_context("Retained hook evidence")
        builder.append_assistant("Earlier answer is summarized")
        await builder.compact()
        url, body = requests[0]
        assert url.endswith("/responses") and body["input"][-1] == {"type": "compaction_trigger"}
        assert body["stream"] is True and body["parallel_tool_calls"] is True
        assert turn.usage.input_tokens == 120 and turn.usage.output_tokens == 15
        assert turn.side_call_records[0]["status"] == "completed"
        window = native_compaction_windows(builder._history[0])[0]
        assert window["output"][-1] == {"type": "compaction", "id": "cmp-0", "encrypted_content": "opaque-0"}
        retained = json.dumps(window["output"], ensure_ascii=False)
        assert "Keep the original task" in retained and "Retained hook evidence" in retained
        assert "Earlier answer is summarized" not in retained and "<environment_context>" not in retained
        expected = adapter._build_responses_input(builder._history)
        repository = ConversationRepository(tmp_path / "conversations")
        record = repository.create_conversation(context_snapshot=builder.export_snapshot())
        recovered = ContextBuilder(llm=adapter)
        recovered.load_snapshot(ConversationRepository(tmp_path / "conversations").get_execution_context(record.id).context_snapshot)
        assert adapter._build_responses_input(recovered._history) == expected
        await recovered.compact()
        assert "Keep the original task" in json.dumps(native_compaction_windows(recovered._history[0]))
        await adapter.aclose()


@pytest.mark.parametrize("count,terminal", [(0, "response.completed"), (2, "response.completed"),
    (1, "response.incomplete"), (1, "")])
@pytest.mark.asyncio
async def test_invalid_compaction_never_replaces_history_and_charges_completed_usage(count, terminal):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response(events(count=count, terminal=terminal)))) as client:
        adapter = OpenAIAdapter(settings(), http_client=client)
        adapter.configure_remote_compaction("v2")
        builder = ContextBuilder(llm=adapter)
        turn = LLMTurnContext()
        builder.bind_llm_turn_context(turn)
        await builder.start_turn("Original", AgentState(user_message="Original"))
        before = deepcopy(builder.export_snapshot()["history"])
        with pytest.raises((RuntimeError, ValueError)):
            await builder.compact()
        assert builder.export_snapshot()["history"] == before
        assert turn.usage.input_tokens == (120 if terminal else 0)
        assert turn.side_call_records[0]["status"] == "failed"
        await adapter.aclose()


@pytest.mark.asyncio
async def test_native_request_failure_keeps_provider_error_details():
    payload = [{"type": "response.failed", "response": {"error": {"type": "invalid_request_error", "message": "unsupported compaction trigger"}}}]
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response(payload))) as client:
        adapter = OpenAIAdapter(settings(), http_client=client)
        adapter.configure_remote_compaction("v2")
        with pytest.raises(ResponsesStreamError, match="unsupported compaction trigger"):
            await adapter.compact_context_v2([LLMMessage("user", "work", is_user_input=True)])
        await adapter.aclose()


@pytest.mark.parametrize("support,expected", [("v2", True), ("unsupported", False)])
@pytest.mark.asyncio
async def test_provider_capability_selects_real_protocol(support, expected):
    config = AppConfig(llm=settings(native_compaction=True), config_layer_stack=ConfigLayerStack((
        ConfigLayer(ConfigLayerSource(kind="user"), {"model_providers": {"custom": {"capabilities": {"remote_compaction": support}}}}),)))
    adapter = create_session_llm(config)
    try:
        assert adapter.remote_compaction_v2 is expected
        assert adapter.capabilities.native_compaction is expected
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_model_can_restrict_the_provider_compaction_capability():
    adapter = OpenAIAdapter(settings(native_compaction=False))
    try:
        adapter.configure_remote_compaction("v2")
        assert not adapter.remote_compaction_v2 and not adapter.capabilities.native_compaction
    finally:
        await adapter.aclose()


def test_retention_budget_preserves_image_groups_and_latest_text():
    adapter = OpenAIAdapter(settings())
    images = [LLMMessage("user", "before", is_user_input=True,
        images=[{"media_type": "image/png", "data": "opaque", "detail": "high"}])]
    # Standard images cost ceil(7373/4), independent of their base64 size.
    assert retained_input(adapter, images, max_tokens=100) == []
    assert retained_input(adapter, images, max_tokens=100, charge_images=False)
    assert retained_input(adapter, images, max_tokens=2000)[0]["content"][-1]["detail"] == "high"
    text = [LLMMessage("user", "old" * 500, is_user_input=True),
        LLMMessage("user", "new" * 500, is_user_input=True)]
    bounded = retained_input(adapter, text, max_tokens=100)
    assert len(bounded) == 1 and "tokens truncated" in bounded[0]["content"][0]["text"]
    raw = ("new" * 500).encode()
    assert bounded[0]["content"][0]["text"] == raw[:200].decode() + "…275 tokens truncated…" + raw[-200:].decode()


def test_agent_inputs_retain_tasks_but_not_descendant_progress_or_completions():
    adapter = OpenAIAdapter(settings())
    items = [agent_message_response_item(author="/root", recipient="/root/child", content="Actual task", trigger_turn=True),
        agent_message_response_item(author="/root/child", recipient="/root", content="progress", trigger_turn=False),
        agent_message_response_item(author="/root/child", recipient="/root", content="finished", trigger_turn=False, message_type="FINAL_ANSWER")]
    messages = [LLMMessage("user", "", input_origin="agent", provider_items=[item]) for item in items]
    assert retained_input(adapter, messages) == [items[0]]
    assert retained_input(adapter, messages, max_tokens=1) == []


def test_boundary_truncation_preserves_positional_metadata_from_older_native_windows():
    from backend.llm.native_compaction import NATIVE_COMPACTION_TYPE

    adapter = OpenAIAdapter(settings())
    message = LLMMessage("assistant", provider_items=[{"type": NATIVE_COMPACTION_TYPE, "origin": adapter.native_context_origin,
        "output": [{"type": "message", "role": "user", "content": [
            {"type": "input_image", "image_url": "data:image/png;base64,opaque", "detail": "high"},
            {"type": "input_text", "text": "newest" * 1000}],
            "internal_chat_message_metadata_passthrough": {"content_item_kinds": ["user.image"], "content_item_metadata": []}},
            {"type": "compaction", "encrypted_content": "old"}]}])
    retained = retained_input(adapter, [message], max_tokens=100)
    assert len(retained) == 1 and len(retained[0]["content"]) == 1
    assert retained[0]["internal_chat_message_metadata_passthrough"] == {
        "content_item_kinds": ["unknown"], "content_item_metadata": [{"provenance": {"type": "unknown"}}]}


@pytest.mark.asyncio
async def test_cancelled_request_closes_the_real_stream_without_installing_output():
    started, stopped = asyncio.Event(), asyncio.Event()

    class Held(httpx.AsyncByteStream):
        async def __aiter__(self):
            started.set()
            await asyncio.Event().wait()
            yield b""

        async def aclose(self):
            stopped.set()

    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200,
            headers={"content-type": "text/event-stream"}, stream=Held()))) as client:
        adapter = OpenAIAdapter(settings(), http_client=client)
        adapter.configure_remote_compaction("v2")
        turn = LLMTurnContext()
        task = asyncio.create_task(adapter.compact_context_v2([LLMMessage("user", "work", is_user_input=True)], turn_context=turn))
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stopped.is_set() and turn.side_call_records[0]["status"] == "cancelled"
        await adapter.aclose()


@pytest.mark.asyncio
async def test_compaction_preserves_history_appended_while_its_request_is_pending():
    started, release = asyncio.Event(), asyncio.Event()

    async def send(_request):
        started.set()
        await release.wait()
        return response(events())

    async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as client:
        adapter = OpenAIAdapter(settings(), http_client=client)
        adapter.configure_remote_compaction("v2")
        builder = ContextBuilder(llm=adapter)
        await builder.start_turn("Original", AgentState(user_message="Original"))
        compact = asyncio.create_task(builder.compact())
        await asyncio.wait_for(started.wait(), 5)
        builder.append_user("Accepted while compaction was running")
        suffix = deepcopy(builder.export_snapshot()["history"][-1])
        release.set()
        await compact
        assert builder.export_snapshot()["history"][-1] == suffix
        assert "Accepted while compaction" not in json.dumps(native_compaction_windows(builder._history[0]))
        await adapter.aclose()


@pytest.mark.asyncio
async def test_background_transport_reads_but_does_not_publish_foreground_route():
    metadata = {"session_id": "same-session", "thread_id": "same-thread", "turn_id": "same-turn"}
    seen = []

    def send(request):
        seen.append(dict(request.headers))
        result = response(events())
        result.headers["x-codex-turn-state"] = "background-only"
        return result

    async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as client:
        adapter = OpenAIAdapter(settings(), http_client=client)
        adapter.configure_remote_compaction("v2")
        await adapter.compact_context_v2([LLMMessage("user", "work", is_user_input=True)], metadata=metadata, background=True)
        assert not adapter._responses_turn_states
        adapter._remember_responses_turn_state(metadata, {"x-codex-turn-state": "foreground-owner"})
        await adapter.compact_context_v2([LLMMessage("user", "work", is_user_input=True)], metadata=metadata, background=True)
        assert seen[-1]["x-codex-turn-state"] == "foreground-owner"
        assert set(adapter._responses_turn_states.values()) == {"foreground-owner"}
        await adapter.aclose()


@pytest.mark.asyncio
async def test_compaction_keeps_captured_endpoint_and_model_across_scheduling(monkeypatch):
    from dataclasses import replace

    sent = []

    def send(request):
        sent.append((str(request.url), json.loads(request.content)))
        return response(events())

    async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as client:
        adapter = OpenAIAdapter(settings(), http_client=client)
        adapter.configure_remote_compaction("v2")
        run = adapter._run_auxiliary_call

        async def change_selection(messages, **kwargs):
            adapter._settings = replace(adapter._settings, base_url="https://later.invalid/v1", model="later-model")
            return await run(messages, **kwargs)

        monkeypatch.setattr(adapter, "_run_auxiliary_call", change_selection)
        replacement = await adapter.compact_context_v2([LLMMessage("user", "work", is_user_input=True)])
        assert sent[0][0] == "https://fixture.invalid/v1/responses"
        assert sent[0][1]["model"] == "fixture-v2"
        assert native_compaction_windows(replacement)[0]["model"] == "fixture-v2"
        await adapter.aclose()
