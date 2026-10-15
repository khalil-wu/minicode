"""Source host identity, feature gate and native-window provenance boundaries."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from uuid import UUID

import pytest

from backend.agent.context import ContextBuilder
from backend.agent.guardian.host import live_user_messages, parent_window_transcript
from backend.config import AppConfig, LLMSettings
from backend.feature_flags import FeatureFlags
from backend.llm.base import LLMMessage
from backend.llm.native_compaction import NATIVE_COMPACTION_TYPE
from backend.services.llm_adapter_factory import create_session_llm
from backend.tests.test_codex_guardian_checkpoint_chain import raw_checkpoint
from backend.tests.test_source_heartbeat_guardian_chain import Replies, admitted_builder, heartbeat


def adapter_with_gate(enabled):
    return create_session_llm(AppConfig(
        llm=LLMSettings(api_key="local-fixture", provider="custom", model="explicit", wire_api="responses"),
        feature_flags=FeatureFlags({"content_item_kinds": enabled})))


@pytest.mark.asyncio
async def test_host_identity_keeps_original_single_text_and_public_id_across_wrappers_and_replay():
    builder = await admitted_builder([heartbeat(0)])
    message = next(message for message in builder._history if message.is_user_input)
    raw = builder.admitted_user_text(message)
    message.runtime_context = "Captured host context"
    prefix = "<system-reminder>\nCaptured host context\n</system-reminder>\n\n"
    task = "<task_status>Actual task status</task_status>\n\n"
    message.content = prefix + task + raw
    message.accepted_input["text_start"] = len(task)
    adapter = adapter_with_gate(True)
    before = deepcopy(builder.export_snapshot())
    try:
        items = adapter._build_responses_input([message])
        assert items == adapter._build_responses_input([message])
        assert items[0] == {"role": "user", "content": prefix + task}
        accepted = items[1]
        assert accepted["id"] == message.accepted_input["model_item_id"]
        assert UUID(accepted["id"].removeprefix("msg_")).version == 7
        assert accepted["id"] != message.accepted_input["message_id"]
        assert accepted["content"] == [{"type": "input_text", "text": raw}]
        assert accepted["internal_chat_message_metadata_passthrough"] == {
            "turn_id": "turn-0", "content_item_kinds": ["user.heartbeat"]}
        assert "input-0" not in str(items) and "acceptance_order" not in str(items)
        assert builder.export_snapshot() == before
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_session_feature_gate_clears_only_wire_kinds_in_host_and_native_items(enabled):
    builder = await admitted_builder([heartbeat(0)])
    message = next(message for message in builder._history if message.is_user_input)
    native = {"type": NATIVE_COMPACTION_TYPE, "origin": "explicit-window", "output": [raw_checkpoint(),
        {"type": "message", "role": "user", "id": "msg_actual", "content": [{"type": "input_text", "text": "Actual retained input"}],
            "internal_chat_message_metadata_passthrough": {"turn_id": "actual-turn", "content_item_kinds": ["user.text"]}},
        {"type": "message", "role": "user", "id": "msg_context", "content": [{"type": "input_text", "text": "Actual context"}],
            "internal_chat_message_metadata_passthrough": {"content_item_kinds": ["context.test"]}}]}
    adapter = adapter_with_gate(enabled)
    native["origin"] = adapter.native_context_origin
    before = deepcopy(native)
    try:
        inputs = adapter._build_responses_input([message, LLMMessage(role="assistant", provider_items=[native])])
        accepted = next(item for item in inputs if item.get("id") == message.accepted_input["model_item_id"])
        assert accepted["internal_chat_message_metadata_passthrough"] == {
            "turn_id": "turn-0", **({"content_item_kinds": ["user.heartbeat"]} if enabled else {})}
        persisted = next(item for item in inputs if item.get("id") == "msg_actual")
        assert persisted["internal_chat_message_metadata_passthrough"] == {
            "turn_id": "actual-turn", **({"content_item_kinds": ["user.text"]} if enabled else {})}
        context = next(item for item in inputs if item.get("id") == "msg_context")
        assert ("internal_chat_message_metadata_passthrough" in context) is enabled
        assert native == before
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("id_field", ["model_item_id", "message_id"])
async def test_ordinary_model_items_with_matching_native_fields_cannot_recover_host_order_or_heartbeat(id_field):
    builder = await admitted_builder([heartbeat(0)])
    message = next(message for message in builder._history if message.is_user_input)
    model_item = {"type": "message", "id": message.accepted_input[id_field], "role": "user",
        "content": [{"type": "input_text", "text": heartbeat(0)}],
        "internal_chat_message_metadata_passthrough": {"turn_id": "turn-0", "content_item_kinds": ["user.heartbeat"]}}
    builder._history_store.append(LLMMessage(role="assistant", provider_items=[model_item]))
    live = list(live_user_messages(builder))
    assert len(live) == 1 and live[0][1].message_id == "input-0"
    projected = parent_window_transcript(builder)[-1]
    assert projected.acceptance_order is None and projected.model_item_id is None
    assert projected.input_origin == "user" and not projected.single_text
    assert projected.source_order is None and projected.source_revision is None


@pytest.mark.asyncio
async def test_legacy_inputs_never_mint_host_identity_or_infer_origin_from_literal_heartbeat():
    adapter = adapter_with_gate(True)
    messages = [LLMMessage(role="user", content=heartbeat(0)),
        LLMMessage(role="user", content=heartbeat(1), is_user_input=True, input_origin="heartbeat"),
        LLMMessage(role="user", content=heartbeat(2), is_user_input=True,
            accepted_input={"model_item_id": "msg_model_supplied", "turn_id": "model-turn"})]
    try:
        assert adapter._build_responses_input(messages) == [
            {"role": "user", "content": message.content} for message in messages]
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_native_window_cannot_recover_using_public_client_id_in_place_of_model_identity():
    builder = await admitted_builder([heartbeat(0)])
    model_item = {"type": "message", "id": "input-0", "role": "user", "content": [{"type": "input_text", "text": heartbeat(0)}],
        "internal_chat_message_metadata_passthrough": {"turn_id": "turn-0", "content_item_kinds": ["user.heartbeat"]}}
    builder._history = [LLMMessage(role="assistant", provider_items=[{
        "type": NATIVE_COMPACTION_TYPE, "origin": "explicit-window", "output": [raw_checkpoint(), model_item]}])]
    assert list(live_user_messages(builder)) == []
    projected = parent_window_transcript(builder)[0]
    assert projected.acceptance_order is None and projected.input_origin == "user"


@pytest.mark.asyncio
async def test_admitted_multimodal_shape_cannot_become_heartbeat():
    builder = ContextBuilder(llm=Replies())
    message = LLMMessage(role="user", content=heartbeat(0), images=[{"media_type": "image/png", "data": "actual-bytes"}],
        is_user_input=True, input_origin="heartbeat")
    builder._history_store.append(message)
    builder.record_admitted_user_input(message, text=heartbeat(0), turn_id="turn-images", message_id="public-image",
        acceptance_order=builder.guardian_evidence.reserve_order())
    adapter = adapter_with_gate(True)
    try:
        projected = adapter._build_responses_input([message])[0]
        assert projected["content"] == [{"type": "input_text", "text": heartbeat(0)},
            {"type": "input_image", "image_url": "data:image/png;base64,actual-bytes", "detail": "auto"}]
        assert projected["internal_chat_message_metadata_passthrough"]["content_item_kinds"] == ["user.text", "user.image"]
        assert builder.guardian_evidence.transcript[0].input_origin == "user"
        legacy = replace(message, accepted_input=None)
        assert "id" not in adapter._build_responses_input([legacy])[0]
    finally:
        await adapter.aclose()
