"""Hosted-search declarations survive settings, discovery and model selection."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend import config_helpers
from backend.llm.capabilities import capabilities_from_openai_settings
from backend.llm.model_runtime import ModelRuntime
from backend.llm.provider_models import ProviderModelsStorage
from backend.services import llm_adapter_factory, llm_provider_helpers, llm_provider_service


@pytest.mark.parametrize("declaration", [None, True, False])
@pytest.mark.parametrize(
    ("provider", "wire_api"),
    [("openai", "responses"), ("custom", "responses"), ("custom", "chat"), ("custom", "anthropic"), ("anthropic", "anthropic")],
)
def test_saved_hosted_search_declaration_reaches_selected_adapter(
    monkeypatch, provider, wire_api, declaration
):
    from backend.llm import anthropic_adapter

    monkeypatch.setattr(llm_adapter_factory, "OpenAIAdapter", lambda *, settings: settings)
    monkeypatch.setattr(anthropic_adapter, "AnthropicAdapter", lambda **kwargs: SimpleNamespace(**kwargs))
    snapshot = {"llm": {"provider": provider, provider: {
        "api_key": "fixture", "base_url": "https://example.invalid/v1", "wire_api": wire_api,
        "model": "selected-model", "model_metadata": {
            "selected-model": {"supports_hosted_web_search": declaration},
            "other-model": {"supports_hosted_web_search": False if declaration is not False else True},
        },
    }}}

    settings = config_helpers.load_llm_settings(snapshot)
    adapter = llm_adapter_factory.build_provider_adapter(provider, settings_snapshot=snapshot)
    other = llm_adapter_factory.build_provider_adapter(provider, "other-model", settings_snapshot=snapshot)

    assert settings.supports_hosted_web_search is declaration
    assert adapter.supports_hosted_web_search is declaration
    assert other.supports_hosted_web_search is (False if declaration is not False else True)
    assert config_helpers.get_llm_settings_payload(snapshot)[provider]["model_metadata"]["selected-model"]["supports_hosted_web_search"] is declaration
    if wire_api in {"chat", "responses"}:
        assert capabilities_from_openai_settings(settings, provider=provider).to_dict()["supports_hosted_web_search"] is declaration


def test_undeclared_custom_endpoint_keeps_automatic_capability():
    snapshot = {"llm": {"provider": "custom", "custom": {
        "api_key": "fixture", "base_url": "https://example.invalid/v1", "wire_api": "responses", "model": "unknown-model",
    }}}

    settings = config_helpers.load_llm_settings(snapshot)

    assert settings.supports_hosted_web_search is None
    assert capabilities_from_openai_settings(settings, provider="custom").supports_hosted_web_search is None


@pytest.mark.parametrize("declaration", [True, False])
@pytest.mark.parametrize("nested", [True, False])
def test_provider_model_discovery_keeps_hosted_search_declaration(declaration, nested):
    capability = {"supports_hosted_web_search": declaration}
    item = {"id": "declared-model", **({"capabilities": capability} if nested else capability)}

    discovery = llm_provider_helpers._extract_model_discovery({"data": [item]})

    assert discovery.model_metadata["declared-model"]["supports_hosted_web_search"] is declaration


@pytest.mark.parametrize("configured", [None, True, False])
def test_model_refresh_preserves_explicit_hosted_search_override(configured):
    incoming = SimpleNamespace(model_metadata={"selected-model": {"supports_hosted_web_search": configured}})

    merged = llm_provider_service._retain_model_behavior(
        {"selected-model": {"supports_hosted_web_search": True}},
        current={}, incoming=incoming, base_url="https://example.invalid/v1", wire_api="responses",
        models=["selected-model"],
    )

    assert merged["selected-model"]["supports_hosted_web_search"] is configured


@pytest.mark.parametrize("declaration", [None, True, False])
@pytest.mark.parametrize("registered", [False, True])
def test_runtime_model_and_adapter_spec_keep_hosted_search_declaration(
    tmp_path, monkeypatch, declaration, registered
):
    monkeypatch.setattr(ModelRuntime, "_load_base_providers", lambda self: {})
    provider = {
        "api": "openai-responses", "api_key": "fixture",
        "models": [{
            "id": "declared-model", "base_url": "https://example.invalid/v1",
            "context_window": 128_000, "max_tokens": 8_000,
            "supports_hosted_web_search": declaration,
        }],
    }
    runtime = ModelRuntime(
        provider_configs={} if registered else {"declared-provider": provider},
        models_store=ProviderModelsStorage(tmp_path / "models-store.json"), settings_snapshot={},
    )
    if registered:
        runtime.register_provider("declared-provider", provider)

    model = runtime.get_model("declared-provider", "declared-model")
    spec = runtime.resolve_adapter_spec("declared-provider", "declared-model")

    assert model.supports_hosted_web_search is declaration
    assert spec.supports_hosted_web_search is declaration
    assert model.to_extension_dict()["supports_hosted_web_search"] is declaration
    if declaration is None:
        assert "supports_hosted_web_search" not in model.to_public_dict()
    else:
        assert model.to_public_dict()["supports_hosted_web_search"] is declaration


@pytest.mark.parametrize("override", [None, False])
def test_runtime_override_can_clear_or_disable_hosted_search(tmp_path, monkeypatch, override):
    monkeypatch.setattr(ModelRuntime, "_load_base_providers", lambda self: {})
    runtime = ModelRuntime(
        provider_configs={"declared-provider": {
            "api": "openai-responses", "api_key": "fixture",
            "models": [{
                "id": "declared-model", "base_url": "https://example.invalid/v1",
                "context_window": 128_000, "max_tokens": 8_000, "supports_hosted_web_search": True,
            }],
            "model_overrides": {"declared-model": {"supports_hosted_web_search": override}},
        }},
        models_store=ProviderModelsStorage(tmp_path / "models-store.json"), settings_snapshot={},
    )

    assert runtime.get_model("declared-provider", "declared-model").supports_hosted_web_search is override
    assert runtime.resolve_adapter_spec("declared-provider", "declared-model").supports_hosted_web_search is override
