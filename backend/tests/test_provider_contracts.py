import pytest

from backend.llm.provider_contracts import (
    ModelDefinition,
    ProviderAdapterSpec,
    ProviderDefinition,
)
from backend.llm.model_registry import ModelRegistry
from backend.llm.model_runtime import ModelRuntime, ProviderRegistrationError


def test_provider_contracts_are_independent_of_model_runtime() -> None:
    model = ModelDefinition(
        provider="openai",
        id="gpt-test",
        name="Test",
        api="openai-responses",
        base_url="https://example.invalid",
    )
    provider = ProviderDefinition(
        id="openai",
        name="OpenAI",
        base_url="https://example.invalid",
        models=(model,),
        configured=True,
        source="builtin",
    )
    spec = ProviderAdapterSpec(
        provider_id=provider.id,
        model_id=model.id,
        api=model.api,
        api_key="",
        base_url=provider.base_url,
        headers={},
        auth_header=False,
        max_tokens=1024,
        model=model,
    )

    assert provider.to_dict()["models"][0]["id"] == "gpt-test"
    assert spec.model is model


def test_model_registry_exposes_only_minicode_method_names() -> None:
    legacy_names = {
        "getError",
        "getAll",
        "getAvailable",
        "hasConfiguredAuth",
        "getApiKeyAndHeaders",
        "getProviderAuthStatus",
        "getProvider",
        "getProviderDisplayName",
        "getProviderAuth",
        "getApiKeyForProvider",
        "isUsingOAuth",
        "registerProvider",
        "registerTransportProvider",
        "unregisterProvider",
        "getRegisteredProviderConfig",
        "getTransportProvider",
        "getRegisteredProviderIds",
    }

    assert legacy_names.isdisjoint(vars(ModelRegistry))


def test_new_model_does_not_inherit_protocol_from_first_base_model() -> None:
    runtime = ModelRuntime()
    base = ModelDefinition(
        provider="custom",
        id="existing",
        name="Existing",
        api="openai-responses",
        base_url="https://first.example/v1",
    )

    with pytest.raises(ProviderRegistrationError, match='no "api" specified'):
        runtime._apply_model_config(
            "custom",
            (base,),
            {"models": [{"id": "new-model", "base_url": "https://new.example/v1"}]},
        )


def test_new_model_uses_explicit_provider_protocol_not_first_model_protocol() -> None:
    runtime = ModelRuntime()
    base = ModelDefinition(
        provider="custom",
        id="existing",
        name="Existing",
        api="openai-responses",
        base_url="https://first.example/v1",
    )

    models = runtime._apply_model_config(
        "custom",
        (base,),
        {
            "api": "anthropic-messages",
            "base_url": "https://messages.example/v1",
            "models": [{"id": "new-model"}],
        },
    )

    assert models[-1].id == "new-model"
    assert models[-1].api == "anthropic-messages"
    assert models[-1].base_url == "https://messages.example/v1"


def test_availability_refresh_failure_clears_stale_snapshot() -> None:
    runtime = ModelRuntime()
    runtime._available_snapshot = (
        ModelDefinition(
            provider="custom",
            id="stale",
            name="Stale",
            api="openai-responses",
            base_url="https://stale.invalid/v1",
        ),
    )

    def broken_compute(*args, apply_filters, apply_model_modifiers):
        del args, apply_filters, apply_model_modifiers
        raise RuntimeError("catalog refresh failed")

    runtime._compute_available = broken_compute  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="catalog refresh failed"):
        runtime.get_available()
    assert runtime.get_available_snapshot() == ()
    assert runtime.get_error() == "Availability refresh: catalog refresh failed"
