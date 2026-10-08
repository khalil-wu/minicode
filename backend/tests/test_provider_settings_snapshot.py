from __future__ import annotations

import pytest

from backend import config_helpers
from backend.services.llm_adapter_factory import build_wire_adapter


@pytest.mark.parametrize("resolver", ["get_image_generation_settings", "get_llm_settings_payload", "load_llm_settings"])
def test_provider_projection_uses_one_config_revision_but_refreshes_next_call(monkeypatch, resolver):
    reads = []

    def read_settings():
        revision = len(reads) + 1
        reads.append(revision)
        return {"llm": {"provider": "custom", "custom": {
            "model": f"model-{revision}", "image_model": f"image-{revision}",
            "base_url": "https://example.invalid/v1", "wire_api": "chat",
        }}}

    monkeypatch.setattr(config_helpers, "_load_effective_settings_json", read_settings)
    resolve = getattr(config_helpers, resolver)
    first, second = resolve(), resolve()
    if resolver == "get_image_generation_settings":
        assert (first["model"], second["model"]) == ("image-1", "image-2")
    elif resolver == "get_llm_settings_payload":
        assert (first["active_model"], second["active_model"]) == ("model-1", "model-2")
        assert first["custom"]["image_model"] == "image-1"
    else:
        assert (first.model, second.model) == ("model-1", "model-2")
    assert reads == [1, 2]


def test_selected_model_metadata_controls_tool_mode():
    settings = {"llm": {"provider": "custom", "custom": {
        "api_key": "fixture", "base_url": "https://example.invalid/v1", "wire_api": "chat",
        "model": "code-model", "model_metadata": {
            "code-model": {"tool_mode": "code_mode_only"},
            "direct-model": {"tool_mode": "direct"},
        },
    }}}

    assert config_helpers.load_llm_settings(settings).tool_mode == "code_mode_only"
    settings["llm"]["custom"]["model"] = "direct-model"
    selected = config_helpers.load_llm_settings(settings)
    assert selected.tool_mode == "direct"
    settings["llm"]["custom"]["wire_api"] = "anthropic"
    adapter = build_wire_adapter(config_helpers.load_llm_settings(settings))
    assert adapter.configured_tool_mode() == "direct"


def test_catalog_and_image_tool_specs_do_not_read_credentials(monkeypatch):
    from types import SimpleNamespace
    from backend.api import routes_agents
    from backend.config_providers import get_available_models, get_models_source
    from backend.llm.model_registry import ModelRegistry
    from backend.llm.model_runtime import ModelRuntime
    from backend.services.llm_config_service import llm_model_updated_payload
    from backend.tools.image_generation_tool import GenerateImageTool

    settings = {"llm": {"provider": "custom", "custom": {
        "model": "selected-model", "available_models": ["selected-model"],
        "image_model": "image-model", "base_url": "https://metadata.invalid/v1",
        "wire_api": "responses", "auth_header": True,
        "model_metadata": {"selected-model": {"context_window": 64000}},
    }}}

    def credential_read_forbidden(*_args, **_kwargs):
        raise AssertionError("Catalog publication must not resolve a provider credential")

    monkeypatch.setattr(config_helpers, "_vault_api_key", credential_read_forbidden)
    monkeypatch.setattr(config_helpers, "_vault_has_scoped_provider_keys", credential_read_forbidden)
    monkeypatch.setattr(ModelRuntime, "has_configured_auth", credential_read_forbidden)
    declared = config_helpers.load_llm_settings(settings, resolve_credentials=False)
    assert declared.model == "selected-model" and declared.context_window == 64000
    assert declared.api_key == "" and declared.auth_header is True
    assert get_available_models("custom", settings) == ["selected-model"]
    assert get_models_source("custom", settings) == ""
    payload = llm_model_updated_payload(provider="custom", selected_model="selected-model",
        available_models=["selected-model"], workspace_root=None, settings_data=settings)
    assert payload["context_window"] == 64000
    catalog = ModelRuntime(settings_snapshot=settings, provider_configs={})
    assert catalog.get_model("custom", "selected-model").context_window == 64000
    assert catalog.provider_payload("custom", "selected-model")["base_url"] == "https://metadata.invalid/v1"
    assert [model.id for model in catalog.get_available_snapshot() if model.provider == "custom"] == ["selected-model"]
    assert ModelRegistry(catalog).get_provider_display_name("custom") == "custom"
    session = SimpleNamespace(is_connected=True, active_conversation_id="metadata-task",
        session_lifecycle=SimpleNamespace(workspace_root_for_conversation=lambda: None),
        _model_runtime_for_conversation=lambda _identity: catalog)
    monkeypatch.setattr(routes_agents._state, "ws_manager", SimpleNamespace(iter_sessions=lambda: [session]))
    assert any(model["model"] == "selected-model" for model in routes_agents._live_agent_model_catalog())
    tool = GenerateImageTool(settings_snapshot=settings, provider="custom")
    assert tool.get_spec().exposure == "core"
    assert tool.get_spec().exposure == "core"

    # A real auth boundary reads the current key, including subsequent edits.
    keys = iter(("first-key", "rotated-key"))
    monkeypatch.setattr(config_helpers, "_provider_api_key_for_base_url", lambda *_args: next(keys))
    assert catalog._raw_api_key("custom") == "first-key"
    assert catalog._raw_api_key("custom") == "rotated-key"
