from __future__ import annotations

import asyncio
import base64
import secrets
import struct
import zlib

import pytest

from backend.agent.context import ContextBuilder
from backend.agent.state import AgentState
from backend.attachments.store import AttachmentStore
from backend.config import AppConfig, LLMSettings, load_llm_settings
from backend.llm.capabilities import capabilities_for_adapter
from backend.llm.model_runtime import ModelRuntime
from backend.llm.openai_adapter import OpenAIAdapter
from backend.services.llm_adapter_factory import build_provider_adapter, create_session_llm


def _marker_png() -> str:
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    image = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    image += chunk(b"tEXt", b"marker\0" + secrets.token_hex(8).encode())
    image += chunk(b"IDAT", zlib.compress(b"\0" + secrets.token_bytes(3))) + chunk(b"IEND", b"")
    return base64.b64encode(image).decode()


def _runtime(tmp_path, source, modalities, model_id="gpt-6.1-sol"):
    metadata = {} if modalities is None else {"input": list(modalities)}
    settings = {"llm": {"provider": "custom", "custom": {
        "model": model_id, "available_models": [model_id], "base_url": "https://controlled.invalid/v1",
        "wire_api": "responses", "auth_header": False, "model_metadata": {model_id: metadata},
    }}}
    definition = {"id": model_id, **metadata}
    provider = {"api": "openai-responses", "base_url": "https://controlled.invalid/v1",
                "auth_header": False, "models": [definition]}
    runtime = ModelRuntime(models_path=tmp_path / "absent-models.json", settings_snapshot=settings,
                           provider_configs={"gateway": provider} if source == "models_json" else {})
    if source == "extension":
        runtime.register_provider("gateway", provider)
    return runtime, "custom" if source == "settings" else "gateway"


@pytest.mark.parametrize("source", ["settings", "models_json", "extension"])
@pytest.mark.parametrize(("modalities", "expected_vision"), [(None, None), (("text",), False), (("text", "image"), True)])
@pytest.mark.parametrize("cold", [False, True])
def test_runtime_declared_image_policy_reaches_responses_after_switch_and_cold_restore(tmp_path, source, modalities, expected_vision, cold):
    async def scenario():
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        owner, encoded = "image-policy-owner", _marker_png()
        store = AttachmentStore(tmp_path / "attachments")
        reference = {"artifact_id": "catalog-image", "kind": "image", "file_name": "image.png", "media_type": "image/png"}
        store.save(artifact_id=reference["artifact_id"], content="Stored image metadata", native_data=encoded,
                   metadata={"conversation_id": owner, "workspace_root": str(workspace), "attachment": reference})
        text_adapter = OpenAIAdapter(settings=LLMSettings(api_key="", provider="custom", model="explicit-text-only",
            base_url="https://controlled.invalid/v1", wire_api="responses", auth_header=False, input_modalities=("text",)))
        builder = ContextBuilder(llm=text_adapter, conversation_id=owner, workspace_root=workspace)
        builder._attachment_store = store
        state = AgentState(user_message="Inspect the uploaded image pixels.", conversation_id=owner, workspace_root=workspace)
        state.attachments = [reference]
        text_request = await builder.build(state.user_message, state)
        assert not text_request[-1].images
        assert "does not support native image input" in text_request[-1].content
        assert "does not support native image input" not in builder._history[-1].content
        runtime, provider = _runtime(tmp_path, source, modalities)
        if cold:
            snapshot = builder.export_snapshot()
            runtime, provider = _runtime(tmp_path, source, modalities)
            builder = ContextBuilder(conversation_id=owner, workspace_root=workspace)
            builder._attachment_store = AttachmentStore(tmp_path / "attachments")
            builder.load_snapshot(snapshot)
        adapter = build_provider_adapter(provider, "gpt-6.1-sol", model_runtime=runtime)
        try:
            assert capabilities_for_adapter(adapter).vision is expected_vision
            assert runtime.provider_payload(provider, "gpt-6.1-sol")["input_modalities"] == list(modalities or ())
            builder.bind_llm(adapter)
            messages = await builder.build(state)
            request = next(message for message in messages if message.attachment_refs)
            wire = adapter._build_responses_input(messages)
            image_parts = [part for item in wire if isinstance(item.get("content"), list)
                           for part in item["content"] if part.get("type") == "input_image"]
            assert len(image_parts) == int(expected_vision is not False)
            assert ("does not support native image input" in request.content) is (expected_vision is False)
            if expected_vision is not False:
                assert image_parts[0]["image_url"] == f"data:image/png;base64,{encoded}"
            assert builder._history[-1].images[0]["data"] == encoded
            assert "does not support native image input" not in builder._history[-1].content
        finally:
            await adapter.aclose()
            await text_adapter.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("model_id", ["gpt-6.1-sol", "arbitrary-provider-model"])
def test_missing_modality_declaration_is_unknown_for_any_builtin_settings_model(tmp_path, model_id):
    runtime, provider = _runtime(tmp_path, "settings", None, model_id)
    assert runtime.get_model(provider, model_id).input == ()


@pytest.mark.parametrize("provider", ["openai", "custom", "anthropic"])
@pytest.mark.parametrize("modalities", [("text",), ("text", "image")])
def test_initial_llm_settings_preserve_explicit_input_modalities(provider, modalities):
    model = "declared-model"
    settings = {"llm": {"provider": provider, provider: {
        "model": model, "available_models": [model], "base_url": "https://controlled.invalid/v1", "auth_header": False,
        "model_metadata": {model: {"input": list(modalities)}},
    }}}
    assert load_llm_settings(settings, resolve_credentials=False).input_modalities == modalities


def test_unlayered_model_switch_does_not_carry_the_previous_text_only_declaration():
    async def scenario():
        config = AppConfig(llm=LLMSettings(api_key="", provider="custom", model="explicit-text-only",
            base_url="https://controlled.invalid/v1", wire_api="responses", auth_header=False, input_modalities=("text",)))
        adapter = create_session_llm(config, model_override="unadvertised-new-model")
        try:
            assert capabilities_for_adapter(adapter).vision is None
            assert adapter._settings.input_modalities == ()
            assert config.llm.input_modalities == ("text",)
        finally:
            await adapter.aclose()
    asyncio.run(scenario())
