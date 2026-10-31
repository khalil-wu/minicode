from __future__ import annotations

import pytest
import backend.config as config

from backend.config import (
    LLMSettings,
    MODEL_CONTEXT_WINDOW_DEFAULT,
    get_provider_model_metadata,
    resolve_context_window,
    resolve_context_window_details,
)
from backend.services.llm_adapter_factory import _openai_compatible_settings
from backend.llm.capabilities import capabilities_from_openai_settings


@pytest.fixture(autouse=True)
def _clear_context_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MINICODE_MAX_CONTEXT_TOKENS", raising=False)


def test_unknown_model_keeps_default() -> None:
    assert resolve_context_window("custom-gateway-model") == MODEL_CONTEXT_WINDOW_DEFAULT
    assert resolve_context_window("") == MODEL_CONTEXT_WINDOW_DEFAULT

    details = resolve_context_window_details("custom-gateway-model")
    assert details.tokens == MODEL_CONTEXT_WINDOW_DEFAULT
    assert details.source == "fallback"
    assert details.verified is False


@pytest.mark.parametrize("model", ["gpt-image-2", "openai/gpt-image-2"])
def test_gpt_image_models_are_routed_to_the_dedicated_images_api(
    model: str,
) -> None:
    capabilities = capabilities_from_openai_settings(
        LLMSettings(
            api_key="test-key",
            provider="custom",
            model=model,
            wire_api="responses",
            reasoning_effort="high",
            reasoning_effort_levels=("high",),
            prompt_cache_retention="24h",
        ),
        provider="custom",
    )

    assert capabilities.streaming is False
    assert capabilities.tool_calling is False
    assert capabilities.parallel_tool_calls is False
    assert capabilities.reasoning_effort_supported is False
    assert capabilities.effective_reasoning_effort == ""
    assert capabilities.vision is False
    assert capabilities.native_pdf is False
    assert capabilities.image_generation is True
    assert capabilities.prompt_caching is False
    assert capabilities.confidence == "known"
    assert "dedicated_image_model_uses_images_api" in capabilities.limitations


def test_gpt_image_model_on_chat_wire_still_uses_the_dedicated_images_api() -> None:
    capabilities = capabilities_from_openai_settings(
        LLMSettings(
            api_key="test-key",
            provider="custom",
            model="gpt-image-2",
            wire_api="chat",
        ),
        provider="custom",
    )

    assert capabilities.tool_calling is False
    assert capabilities.image_generation is True
    assert "dedicated_image_model_uses_images_api" in capabilities.limitations


def test_unknown_image_named_gateway_model_remains_capability_unknown() -> None:
    capabilities = capabilities_from_openai_settings(
        LLMSettings(
            api_key="test-key",
            provider="custom",
            model="team-image-generator",
            wire_api="responses",
        ),
        provider="custom",
    )

    assert capabilities.tool_calling is None
    assert capabilities.image_generation is None


def test_known_family_uses_application_default() -> None:
    assert resolve_context_window("claude-3-5-sonnet") == 272_000
    assert resolve_context_window("gpt-4o") == 272_000
    assert resolve_context_window("deepseek-r1") == 272_000

    details = resolve_context_window_details("gpt-4o")
    assert details.source == "fallback"
    assert details.verified is False


def test_provider_metadata_beats_known_model_table() -> None:
    details = resolve_context_window_details(
        "gpt-4o",
        provider_context_window=96_000,
    )

    assert details.tokens == 96_000
    assert details.source == "provider"
    assert details.verified is True


def test_host_override_beats_provider_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINICODE_MAX_CONTEXT_TOKENS", "64000")

    details = resolve_context_window_details(
        "gpt-4o",
        provider_context_window=96_000,
    )

    assert details.tokens == 64_000
    assert details.source == "host_override"
    assert details.verified is True


def test_selected_model_metadata_does_not_leak_across_model_override() -> None:
    section = {
        "api_key": "test-key",
        "base_url": "https://gateway.example/v1",
        "model": "model-a",
        "wire_api": "responses",
        "reasoning_effort": "high",
        "model_metadata": {
            "model-a": {
                "context_window": 128_000,
                "reasoning_effort_levels": ["low", "high"],
            }
        },
    }

    selected = get_provider_model_metadata(section, "model-b")
    settings = _openai_compatible_settings(
        section,
        provider="custom",
        model_override="model-b",
    )

    assert selected["reasoning_effort_levels"] == []
    assert selected["context_window_source"] == "fallback"
    assert selected["context_window_verified"] is False
    assert settings.model == "model-b"
    assert settings.reasoning_effort_levels == ()
    assert settings.context_window == MODEL_CONTEXT_WINDOW_DEFAULT
    assert settings.context_window_source == "fallback"
    assert settings.context_window_verified is False

    capabilities = capabilities_from_openai_settings(settings, provider="custom")
    assert capabilities.configured_reasoning_effort == "high"
    assert capabilities.effective_reasoning_effort == ""
    assert capabilities.reasoning_effort_supported is False
    assert capabilities.context_window == MODEL_CONTEXT_WINDOW_DEFAULT
    assert capabilities.context_window_source == "fallback"
    assert capabilities.context_window_verified is False
    assert "context_window_fallback_unverified" in capabilities.limitations


def test_current_openai_models_use_the_source_catalog_window() -> None:
    assert resolve_context_window("gpt-5.6-sol") == 272_000
    assert resolve_context_window("gpt-5.6-terra") == 272_000
    assert resolve_context_window("gpt-5.5") == 272_000
    assert resolve_context_window("gpt-5.4-mini") == 272_000


@pytest.mark.parametrize("model,maximum", [
    ("gpt-6.1-sol", 872_000), ("gpt-6-sol", 872_000), ("gpt-6-astra", 872_000),
    ("gpt-6-luna", 872_000), ("gpt-5.6-sol", 872_000), ("gpt-5.6-terra", 872_000),
    ("gpt-5.6-luna", 872_000), ("gpt-5.5", 272_000),
])
def test_source_model_windows_and_overrides_respect_the_catalog_maximum(monkeypatch, model, maximum):
    metadata = get_provider_model_metadata({}, model)
    assert metadata["context_window"] == 272_000
    assert metadata["context_window_source"] == "known_model"
    assert metadata["context_window_verified"] is True
    assert metadata["max_context_window"] == maximum
    monkeypatch.setenv("MINICODE_MAX_CONTEXT_TOKENS", "1000000")
    assert resolve_context_window(model) == maximum


def test_provider_context_override_cannot_exceed_its_declared_maximum():
    details = resolve_context_window_details("gateway-model", provider_context_window=100_000,
                                             provider_max_context_window=64_000)
    assert details.tokens == 64_000 and details.source == "provider"


@pytest.mark.parametrize("model", ["gateway-model", "gpt-6-sol"])
def test_declared_provider_maximum_also_bounds_model_default(model):
    details = resolve_context_window_details(model, provider_max_context_window=64_000)
    assert details.tokens == 64_000


@pytest.mark.parametrize("model,window,maximum", [
    ("gpt-daybreak-blue-latest", 272_000, 872_000),
    ("gpt-daybreak-red-latest", 372_000, 372_000),
    ("codex-auto-review", 272_000, 872_000),
    ("gpt-6-sol-snapshot", 272_000, 872_000),
])
def test_source_catalog_windows_and_prefix_lookup(model, window, maximum):
    metadata = get_provider_model_metadata({}, model)
    assert (metadata["context_window"], metadata["max_context_window"]) == (window, maximum)


def test_custom_responses_settings_use_exact_known_gpt56_reasoning_levels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        config,
        "_provider_api_key_for_base_url",
        lambda _provider, _base_url: "test-key",
    )
    settings_data = {
        "llm": {
            "provider": "custom",
            "custom": {
                "base_url": "https://gateway.example/v1",
                "model": "gpt-5.6-sol",
                "wire_api": "responses",
                "reasoning_effort": "max",
                "reasoning_effort_levels": [],
                "model_metadata": {},
            },
        }
    }

    settings = config.get_custom_settings(settings_data)

    assert settings["reasoning_effort_levels"] == [
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
        "ultra",
    ]
    assert settings["configured_reasoning_effort"] == "max"
    assert settings["effective_reasoning_effort"] == "max"
    assert settings["reasoning_effort_supported"] is True


def test_provider_namespaced_model_resolves_terminal_model_id() -> None:
    assert resolve_context_window("openai/gpt-5.6-sol") == 272_000
    assert resolve_context_window("openrouter/openai/gpt-5.4-mini") == 272_000


@pytest.mark.parametrize("model", ["gpt-6-astra", "openai/gpt-6-astra"])
def test_astra_reasoning_controls_resolve_without_gateway_capability_metadata(model):
    settings = _openai_compatible_settings({"api_key": "test-key", "base_url": "http://127.0.0.1:8317/v1",
        "model": model, "wire_api": "responses", "model_metadata": {}}, provider="custom", model_override=model)
    capabilities = capabilities_from_openai_settings(settings, provider="custom")
    assert capabilities.reasoning_effort_supported is True
    assert capabilities.reasoning_effort_levels == ("low", "medium", "high", "xhigh", "max", "ultra")
    assert capabilities.effective_reasoning_effort == "low"
    assert capabilities.max_context_window == 872_000


def test_small_local_models_follow_requested_default() -> None:
    assert resolve_context_window("llama-3-8b") == 272_000
    assert resolve_context_window("gemma-7b") == 272_000
    assert resolve_context_window("phi-4") == 272_000


@pytest.mark.parametrize("model", ["gpt-6-luna", "openai/gpt-6-luna"])
def test_luna_explicit_reasoning_reaches_responses_request(model):
    from backend.llm.openai_adapter import _responses_reasoning_request
    settings = _openai_compatible_settings({"api_key": "test-key", "base_url": "http://127.0.0.1:8317/v1",
        "model": model, "wire_api": "responses", "reasoning_effort": "high", "model_metadata": {}},
        provider="custom", model_override=model)
    capabilities = capabilities_from_openai_settings(settings, provider="custom")
    assert capabilities.effective_reasoning_effort == "high"
    assert capabilities.max_context_window == 872_000
    assert _responses_reasoning_request(settings)["effort"] == "high"


def test_family_variants_share_the_same_default() -> None:
    assert resolve_context_window("llama-3.1-70b") == 272_000


def test_1m_suffix_does_not_declare_provider_capacity() -> None:
    assert resolve_context_window("claude-opus-4-1m") == 272_000
    assert resolve_context_window("claude-3-5-sonnet-1m") == 272_000


def test_env_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINICODE_MAX_CONTEXT_TOKENS", "32000")
    assert resolve_context_window("claude-opus-4") == 32_000
    assert resolve_context_window("custom-model") == 32_000
