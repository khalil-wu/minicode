from __future__ import annotations

import json
from dataclasses import replace

import httpx
import pytest

from backend.agent.context import ContextBuilder
from backend.agent.model_execution import ModelExecutionSnapshot
from backend.agent.run_context import RunContext
from backend.config import AgentSettings, AppConfig, LLMSettings, TokenBudget
from backend.agent.state import AgentState
from backend.llm.base import LLMMessage
from backend.llm.anthropic_adapter import AnthropicAdapter
from backend.llm.native_compaction import NativeContextCompatibilityError, native_compaction_windows
from backend.llm.openai_adapter import OpenAIAdapter
from backend.tests.test_core_model_audit_closure import Turn, seed


def responses_answer(text):
    event = {"type": "response.completed", "response": {
        "id": "audit-response", "status": "completed", "output": [{
            "id": "answer", "type": "message", "role": "assistant", "phase": "final_answer",
            "content": [{"type": "output_text", "text": text}],
        }], "usage": {"input_tokens": 30, "output_tokens": 5},
    }}
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, text="data: " + json.dumps(event) + "\n\n")


def anthropic_answer():
    events = [
        {"type": "message_start", "message": {"id": "target-answer", "role": "assistant", "model": "small", "content": [], "usage": {"input_tokens": 30, "output_tokens": 0}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Target model completed the task."}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 5}},
        {"type": "message_stop"},
    ]
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, text="".join("data: " + json.dumps(event) + "\n\n" for event in events))


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["anthropic", "other-responses", "same-responses"])
async def test_native_previous_model_downshift_installs_context_consumable_by_target(tmp_path, target):
    requests = []

    def respond(request):
        body = json.loads(request.content)
        requests.append((request.url.host, request.url.path, body))
        if request.url.path.endswith("/compact"):
            return httpx.Response(200, json={"id": "compact", "output": [
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Preserve A17 and continue."}]},
                {"type": "compaction", "encrypted_content": "original-provider-state"},
            ], "usage": {"input_tokens": 40000, "output_tokens": 100}})
        if request.url.host == "anthropic.invalid":
            return anthropic_answer()
        answer = "Target model completed the task." if body["model"] == "small" else "Summary: preserve A17 and finish the original task."
        return responses_answer(answer)

    previous_settings = LLMSettings(api_key="fixture", model="large", base_url="https://previous.invalid/v1", wire_api="responses", native_compaction=True, context_window=100000)
    settings = AgentSettings(max_iterations=3, code_mode_only=False, stream_max_attempts=0, compaction_keep_recent_tokens=128)
    budget = TokenBudget(total=20000, response_reserve=1024)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        previous = OpenAIAdapter(previous_settings, http_client=client)
        if target == "anthropic":
            selected = AnthropicAdapter(api_key="fixture", model="small", base_url="https://anthropic.invalid/v1", context_window=20000)
            selected._http_client = client
        else:
            selected = OpenAIAdapter(replace(previous_settings, model="small", context_window=20000,
                base_url="https://previous.invalid/v1" if target == "same-responses" else "https://other.invalid/v1"), http_client=client)
        config = AppConfig(llm=replace(previous_settings, model="small"), agent=settings, token_budget=budget)
        owner = RunContext(model_execution=ModelExecutionSnapshot(config, selected, target, "small"))
        turn = Turn(tmp_path / target, selected, owner=owner, settings=settings)
        turn.context.bind_llm(previous)
        seed(turn.context, 80)
        try:
            await turn.run()
            assert turn.state.terminal_status == "completed", turn.state.stopped_reason
            assert turn.state.reply == "Target model completed the task."
            assert turn.context._llm is selected
            assert turn.context._budget.total == 20000
            assert any(event.type == "context_compacted" for event in turn.events)
            selected.validate_context(turn.context._history)
            if target == "same-responses":
                assert any(path.endswith("/compact") for _, path, _ in requests)
                assert any(native_compaction_windows(message) for message in turn.context._history)
            else:
                assert not any(path.endswith("/compact") for _, path, _ in requests)
                assert not any(native_compaction_windows(message) for message in turn.context._history)
                assert any(body["model"] == "large" for _, _, body in requests)
                target_input = next(body for host, _, body in requests if body["model"] == "small")
                assert "A17" in json.dumps(target_input)
        finally:
            await turn.close()
            await previous.aclose()


@pytest.mark.asyncio
async def test_incompatible_existing_native_window_is_rejected_without_rewriting_history(tmp_path):
    def compact_response(request):
        return httpx.Response(200, json={"output": [{"type": "compaction", "encrypted_content": "opaque"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(compact_response)) as client:
        previous = OpenAIAdapter(LLMSettings(api_key="fixture", model="large", base_url="https://previous.invalid/v1", wire_api="responses", native_compaction=True), http_client=client)
        target = AnthropicAdapter(api_key="fixture", model="small")
        builder = ContextBuilder(llm=previous)
        builder.append_user("Preserve A17")
        await builder.compact()
        before = builder.export_snapshot()
        with pytest.raises(ValueError, match="original provider"):
            await builder.compact(replacement_llm=target, replacement_budget=TokenBudget(total=20000))
        assert builder.export_snapshot() == before
        assert builder._llm is previous
        await target.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("item_type", ["message", "function_call_output", "custom_tool_call_output"])
async def test_native_replacement_media_is_validated_before_installation(item_type):
    def image_compaction(request):
        image = {"type": "input_image", "image_url": "data:image/png;base64,AAAA"}
        image_item = {"type": "message", "role": "user", "content": [image]} if item_type == "message" else {
            "type": item_type, "call_id": "old-image", "output": [image],
        }
        return httpx.Response(200, json={"output": [
            image_item,
            {"type": "compaction", "encrypted_content": "opaque"},
        ]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(image_compaction)) as client:
        previous_settings = LLMSettings(api_key="fixture", model="large", base_url="https://previous.invalid/v1", wire_api="responses", native_compaction=True)
        previous = OpenAIAdapter(previous_settings, http_client=client)
        target = OpenAIAdapter(replace(previous_settings, model="text-only", input_modalities=("text",)), http_client=client)
        builder = ContextBuilder(llm=previous)
        await builder.start_turn("Preserve A17", AgentState(user_message="Preserve A17"))
        before = builder.export_snapshot()
        with pytest.raises(NativeContextCompatibilityError, match="retains image inputs"):
            await builder.compact(replacement_llm=target, replacement_budget=TokenBudget(total=20000))
        assert builder.export_snapshot() == before
        assert builder._compaction_count == 0
        assert builder._budget.total == 1000000


@pytest.mark.asyncio
async def test_native_image_model_mismatch_preserves_history_and_reports_capability(tmp_path):
    def image_compaction(request):
        return httpx.Response(200, json={"output": [
            {"type": "message", "role": "user", "content": [{"type": "input_image", "image_url": "data:image/png;base64,AAAA"}]},
            {"type": "compaction", "encrypted_content": "opaque"},
        ]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(image_compaction)) as client:
        previous_settings = LLMSettings(api_key="fixture", model="large", base_url="https://previous.invalid/v1", wire_api="responses", native_compaction=True)
        previous = OpenAIAdapter(previous_settings, http_client=client)
        target = OpenAIAdapter(replace(previous_settings, model="text-only", input_modalities=("text",)), http_client=client)
        turn = Turn(tmp_path / "native-image", target)
        builder = turn.context
        builder.bind_llm(previous)
        builder.append_user("Preserve A17")
        await builder.compact()
        native_before = [item for message in builder._history for item in message.provider_items]
        builder.bind_llm(target)
        try:
            await turn.run()
            assert turn.state.terminal_status == "failed"
            assert turn.state.stopped_reason == "provider_capability"
            assert any(event.type == "error" and event.data.get("error_code") == "native_context_incompatible"
                and "retains image inputs" in event.data["message"] for event in turn.events)
            assert [item for message in builder._history for item in message.provider_items] == native_before
        finally:
            await turn.close()
            await previous.aclose()


@pytest.mark.asyncio
async def test_ordinary_media_is_projected_before_native_validation():
    previous_settings = LLMSettings(api_key="fixture", model="text-only", base_url="https://previous.invalid/v1", wire_api="responses", input_modalities=("text",))
    target = OpenAIAdapter(previous_settings)
    builder = ContextBuilder(llm=target)
    window = {"type": "responses_compaction", "origin": target.native_context_origin,
        "output": [{"type": "compaction", "encrypted_content": "opaque"}]}
    builder._history_store.append(LLMMessage(role="assistant", provider_items=[window]))
    builder._history_store.append(LLMMessage(role="user", content="Continue the text task", images=[{"media_type": "image/png", "data": "AAAA"}], is_user_input=True))
    try:
        messages = await builder.build(AgentState(user_message="Continue the text task"))
        assert not any(message.images for message in messages)
        assert builder._history[-1].images
        assert window in messages[-2].provider_items
    finally:
        await target.aclose()
