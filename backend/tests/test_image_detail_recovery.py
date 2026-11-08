from __future__ import annotations

import pytest

from backend.agent.context import ContextBuilder
from backend.agent.state import AgentState
from backend.config import LLMSettings
from backend.llm.base import LLMMessage
from backend.llm.openai_adapter import OpenAIAdapter, _openai_chat_messages


@pytest.mark.asyncio
@pytest.mark.parametrize("detail", [None, "auto", "low", "high", "original"])
@pytest.mark.parametrize("stored", [False, True])
async def test_image_detail_survives_snapshot_hydration_and_actual_provider_projection(tmp_path, detail, stored):
    image = {"media_type": "image/png", "data": "aW1hZ2U="}
    if detail is not None:
        image["detail"] = detail
    refs = []
    if stored:
        image["artifact_id"] = "owned-image"
        refs = [{"artifact_id": "owned-image", "kind": "image", "media_type": "image/png", "file_name": "owned.png"}]
    builder = ContextBuilder(conversation_id="owner", workspace_root=tmp_path)
    builder._history_store.append(LLMMessage("user", "Look at the actual pixels", images=[image], attachment_refs=refs))
    adapter = OpenAIAdapter(LLMSettings(api_key="", provider="openai", wire_api="responses"))
    try:
        expected_responses = adapter._build_responses_input(builder._history)
        expected_chat = _openai_chat_messages(builder._history)
        snapshot = builder.export_snapshot()
        assert snapshot["history"][0]["images"][0].get("detail") == detail
        restored = ContextBuilder(conversation_id="owner", workspace_root=tmp_path)
        restored.load_snapshot(snapshot)
        if stored:
            class Store:
                def get_payload(self, artifact_id, *, conversation_id, workspace_root):
                    assert (artifact_id, conversation_id, workspace_root) == ("owned-image", "owner", str(tmp_path))
                    return {"native_data": "aW1hZ2U=", "metadata": {"attachment": refs[0]}}
            restored._attachment_store = Store()
            restored._rehydrate_attachment_refs(AgentState(user_message="continue", conversation_id="owner"), tmp_path)
        assert restored._history[0].images == builder._history[0].images
        assert adapter._build_responses_input(restored._history) == expected_responses
        assert _openai_chat_messages(restored._history) == expected_chat
    finally:
        await adapter.aclose()
