from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from backend.config import LLMSettings, SettingsError, _normalize_provider, _provider_request_material
from backend.llm.anthropic_adapter import AnthropicAdapter
from backend.llm.model_runtime import ModelRuntime, ProviderAdapterSpec
from backend.llm.openai_adapter import OpenAIAdapter
from backend.services.llm_adapter_factory import (
    _build_registered_provider_adapter,
    build_provider_adapter,
    build_wire_adapter,
    create_session_llm,
)
from backend.services.llm_provider_helpers import (
    _normalize_provider_value,
    _select_refreshed_model,
)


def _anthropic_spec(**updates: object) -> ProviderAdapterSpec:
    base = ProviderAdapterSpec(
        provider_id="anthropic",
        model_id="claude-opus-5",
        api="anthropic-messages",
        api_key="test-key",
        base_url="https://api.anthropic.com/v1",
        headers={},
        auth_header=False,
        max_tokens=8_000,
    )
    return replace(base, **updates)


def _openai_spec(**updates: object) -> ProviderAdapterSpec:
    base = ProviderAdapterSpec(
        provider_id="custom",
        model_id="gateway-model",
        api="openai-responses",
        api_key="test-key",
        base_url="https://gateway.example/v1",
        headers={"X-Gateway": "test"},
        auth_header=True,
        max_tokens=16_000,
        small_fast_model="gateway-model-mini",
        reasoning_effort="high",
        responses_reasoning_summary="auto",
        prompt_cache_retention="24h",
        reasoning_effort_levels=("low", "medium", "high"),
        context_window=200_000,
        context_window_source="provider",
        context_window_verified=True,
    )
    return replace(base, **updates)


def test_model_runtime_custom_anthropic_gateway_uses_messages_transport() -> None:
    adapter = _build_registered_provider_adapter(
        _anthropic_spec(
            provider_id="custom",
            base_url="https://gateway.example",
        )
    )

    assert adapter._provider_id == "custom"
    assert not hasattr(adapter, "_use_raw_http")


def test_model_runtime_first_party_anthropic_uses_same_messages_transport() -> None:
    adapter = _build_registered_provider_adapter(_anthropic_spec())

    assert adapter._provider_id == "anthropic"
    assert not hasattr(adapter, "_use_raw_http")


def test_model_runtime_anthropic_preserves_direct_proxy_mode() -> None:
    adapter = _build_registered_provider_adapter(
        _anthropic_spec(proxy_mode="direct")
    )

    assert adapter._proxy_mode == "direct"


def test_anthropic_model_advertises_only_configured_thinking_capability() -> None:
    enabled = ModelRuntime._base_model(
        "anthropic",
        "claude-opus-5",
        api="anthropic-messages",
        base_url="https://api.anthropic.com/v1",
        max_tokens=8_000,
        settings={"thinking_budget": 2_048},
    )
    disabled = ModelRuntime._base_model(
        "anthropic",
        "claude-opus-5",
        api="anthropic-messages",
        base_url="https://api.anthropic.com/v1",
        max_tokens=8_000,
        settings={"thinking_budget": 0},
    )

    assert enabled.reasoning is True
    assert enabled.thinking_level_map is None
    assert disabled.reasoning is False


def test_model_runtime_extension_anthropic_provider_uses_messages_transport() -> None:
    adapter = _build_registered_provider_adapter(
        _anthropic_spec(
            provider_id="team-gateway",
            base_url="https://gateway.example",
            extension_defined=True,
        )
    )

    assert adapter._provider_id == "team-gateway"
    assert not hasattr(adapter, "_use_raw_http")


def test_model_runtime_openai_responses_preserves_gateway_contract() -> None:
    adapter = _build_registered_provider_adapter(_openai_spec())

    settings = adapter._settings
    assert settings.provider == "custom"
    assert settings.wire_api == "responses"
    assert settings.base_url == "https://gateway.example/v1"
    assert settings.model == "gateway-model"
    assert settings.small_fast_model == "gateway-model-mini"
    assert settings.default_headers == (("X-Gateway", "test"),)
    assert settings.auth_header is True
    assert not hasattr(settings, "responses_stateful_continuation")
    assert settings.context_window == 200_000
    assert settings.context_window_verified is True
    assert settings.max_tokens == 16_000


def test_model_runtime_openai_preserves_direct_proxy_mode() -> None:
    adapter = _build_registered_provider_adapter(
        _openai_spec(proxy_mode="direct")
    )

    assert adapter._settings.proxy_mode == "direct"


@pytest.mark.parametrize("api", ["openai-responses", "openai-completions"])
def test_model_runtime_openai_auto_output_limit_stays_unset(api: str) -> None:
    adapter = _build_registered_provider_adapter(
        _openai_spec(api=api, max_tokens=0)
    )

    assert adapter._settings.max_tokens == 0


def test_model_runtime_anthropic_auto_output_limit_uses_required_default() -> None:
    adapter = _build_registered_provider_adapter(
        _anthropic_spec(max_tokens=0)
    )

    assert adapter._max_tokens == 8_000


def test_model_runtime_openai_completions_selects_chat_transport() -> None:
    adapter = _build_registered_provider_adapter(
        _openai_spec(
            api="openai-completions",
        )
    )

    settings = adapter._settings
    assert settings.wire_api == "chat"
    assert not hasattr(settings, "responses_stateful_continuation")
    assert settings.base_url == "https://gateway.example/v1"
    assert settings.default_headers == (("X-Gateway", "test"),)


def test_explicit_anthropic_wire_uses_production_custom_messages_adapter() -> None:
    adapter = build_wire_adapter(
        LLMSettings(
            api_key="test-key",
            provider="custom",
            base_url="https://gateway.example",
            model="gateway-model",
            small_fast_model="gateway-model-fast",
            max_tokens=0,
            wire_api="anthropic",
            default_headers=(("X-Gateway", "test"),),
        ),
        provider_id="custom",
    )

    assert isinstance(adapter, AnthropicAdapter)
    assert adapter._provider_id == "custom"
    assert not hasattr(adapter, "_use_raw_http")
    assert adapter._model == "gateway-model"
    assert adapter._small_fast_model == "gateway-model-fast"
    assert adapter._max_tokens == 8_000
    assert adapter._default_headers == {"X-Gateway": "test"}
    assert adapter._messages_url() == "https://gateway.example/v1/messages"


@pytest.mark.parametrize("wire_api", ["chat", "responses"])
def test_explicit_openai_wire_keeps_openai_adapter(wire_api: str) -> None:
    settings = LLMSettings(
        api_key="test-key",
        provider="custom",
        base_url="https://gateway.example/v1",
        model="gateway-model",
        wire_api=wire_api,
    )

    adapter = build_wire_adapter(settings)

    assert isinstance(adapter, OpenAIAdapter)
    assert adapter._settings is settings


def test_explicit_wire_factory_rejects_unknown_protocol() -> None:
    with pytest.raises(ValueError, match="Unsupported LLM wire API"):
        build_wire_adapter(
            LLMSettings(
                api_key="test-key",
                model="gateway-model",
                wire_api="invented-protocol",
            )
        )


def test_factory_preserves_configured_headers_from_settings_projection(monkeypatch) -> None:
    monkeypatch.setattr(
        "backend.services.llm_adapter_factory.get_openai_settings",
        lambda: {
            "api_key": "",
            "base_url": "https://relay.example/v1",
            "model": "relay-model",
            "wire_api": "chat",
            "default_headers": (("X-Relay", "enabled"),),
            "auth_header": False,
        },
    )

    adapter = build_provider_adapter("openai")

    assert isinstance(adapter, OpenAIAdapter)
    assert adapter._settings.default_headers == (("X-Relay", "enabled"),)


def test_factory_does_not_translate_provider_construction_errors_to_auth_errors(monkeypatch) -> None:
    def broken_settings():
        raise ValueError("explicit provider configuration is invalid")

    monkeypatch.setattr(
        "backend.services.llm_adapter_factory.get_openai_settings",
        broken_settings,
    )

    with pytest.raises(ValueError, match="explicit provider configuration is invalid"):
        build_provider_adapter("openai")


def test_factory_rejects_unknown_provider_without_openai_substitution() -> None:
    with pytest.raises(ValueError, match="Unknown LLM provider"):
        build_provider_adapter("provider-that-does-not-exist")


def test_model_runtime_factory_requires_explicit_model_selection() -> None:
    runtime = SimpleNamespace(
        get_models=lambda _provider: (SimpleNamespace(id="directory-first"),),
        resolve_adapter_spec=lambda *_args: (_ for _ in ()).throw(
            AssertionError("factory must not select the first registered model")
        ),
    )

    with pytest.raises(ValueError, match="explicit model selection"):
        build_provider_adapter("team-provider", model_runtime=runtime)


def test_session_factory_builds_only_the_explicit_provider(monkeypatch) -> None:
    selected = object()
    calls: list[tuple[str, str | None]] = []

    def build(provider: str, model_override: str | None = None, **_kwargs):
        calls.append((provider, model_override))
        return selected

    monkeypatch.setattr(
        "backend.services.llm_adapter_factory.build_provider_adapter",
        build,
    )
    monkeypatch.setattr(
        "backend.services.llm_adapter_factory.get_llm_provider",
        lambda: "openai",
    )
    stale_config = SimpleNamespace(
        agent=SimpleNamespace(fallback_providers=("anthropic", "custom")),
    )

    adapter = create_session_llm(stale_config, model_override="gpt-test")

    assert adapter is selected
    assert calls == [("openai", "gpt-test")]


def test_unknown_provider_values_fail_closed_at_each_config_boundary() -> None:
    with pytest.raises(SettingsError, match="Unknown LLM provider"):
        _normalize_provider("vendor-that-is-not-registered")
    with pytest.raises(ValueError, match="Unknown LLM provider"):
        _normalize_provider_value("vendor-that-is-not-registered")


def test_provider_request_material_rejects_nul_header_values() -> None:
    with pytest.raises(SettingsError, match="control separator"):
        _provider_request_material({"headers": {"X-Relay": "bad\x00value"}})


def test_model_refresh_preserves_an_explicit_model_when_discovery_drops_it() -> None:
    assert _select_refreshed_model(
        "custom",
        ["new-model"],
        "explicit-model-no-longer-advertised",
    ) == "explicit-model-no-longer-advertised"


def test_small_fast_model_requires_explicit_configuration() -> None:

    official = AnthropicAdapter(
        api_key="sk-test",
        model="claude-opus-4-6",
        provider_id="anthropic",
    )
    with pytest.raises(RuntimeError, match="explicit configured model"):
        official.small_fast_model_id()

    gateway = AnthropicAdapter(
        api_key="sk-test",
        model="claude-sonnet-4-6",
        base_url="https://gw.example.test",
        provider_id="custom",
    )
    with pytest.raises(RuntimeError, match="explicit configured model"):
        gateway.small_fast_model_id()

    # An OpenAI-wire proxy relaying a claude model must not be sent an
    # Anthropic model id either.
    proxied = OpenAIAdapter(
        LLMSettings(
            api_key="test",
            provider="custom",
            base_url="https://gw.example.test/v1",
            model="claude-sonnet-4-6",
            wire_api="chat",
        )
    )
    with pytest.raises(RuntimeError, match="explicit configured model"):
        proxied.small_fast_model_id()

    # An explicitly configured small/fast model always wins.
    configured = AnthropicAdapter(
        api_key="sk-test",
        model="claude-sonnet-4-6",
        small_fast_model="gateway-haiku",
        provider_id="custom",
    )
    assert configured.small_fast_model_id() == "gateway-haiku"
