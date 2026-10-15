from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from backend.config_helpers import LLMSettings, SettingsError, get_provider_model_metadata
from backend.llm.model_runtime import ModelRuntime
from backend.llm.provider_contracts import ModelDefinition, ProviderRegistrationError
from backend.llm.source_context_metadata import source_context_metadata
from backend.services.llm_adapter_factory import _build_registered_provider_adapter
from backend.tests.test_model_runtime import _FakeAuthStorage


def messages(label="source", *, enabled=True):
    return {"token_budget": {"enabled": enabled, "use_history_notes_extension": False,
        "reminder_threshold_tokens": 6000, "reminder_message_template": label + " {n_remaining}",
        "guidance_message": label, "auto_compact_fallback_prompt": label,
        "auto_compact_fallback_buffer_tokens": 1000}}


def test_source_metadata_is_preserved_from_settings_to_base_catalog_and_is_detached():
    raw = messages()
    settings = {"model": "source-model", "base_url": "https://fixture.invalid/v1", "api_key": "fixture",
        "model_metadata": {"source-model": {"model_messages": raw, "supports_experimental_context": True}}}
    runtime = ModelRuntime(provider_configs={}, settings_snapshot={"llm": {"provider": "openai", "openai": settings}})
    selected = runtime.get_model("openai", "source-model")
    assert selected.model_messages == raw and selected.supports_experimental_context
    raw["token_budget"]["guidance_message"] = "changed outside catalog"
    assert selected.model_messages["token_budget"]["guidance_message"] == "source"
    copied = selected.to_extension_dict()
    copied["model_messages"]["token_budget"]["guidance_message"] = "changed by extension input"
    assert selected.model_messages["token_budget"]["guidance_message"] == "source"


def test_config_models_overrides_and_adapter_keep_owned_messages():
    before, after = messages("before"), messages("after")
    runtime = ModelRuntime(provider_configs={"owned": {"api": "openai-responses", "base_url": "https://fixture.invalid/v1", "api_key": "fixture",
        "models": [{"id": "m", "model_messages": before, "supports_experimental_context": False}],
        "model_overrides": {"m": {"model_messages": after, "supports_experimental_context": True}}}})
    selected = runtime.get_model("owned", "m")
    assert selected.model_messages == after and selected.supports_experimental_context
    spec = runtime.resolve_adapter_spec("owned", "m")
    adapter = _build_registered_provider_adapter(spec)
    assert adapter._settings.model_messages == after
    assert adapter._settings.supports_experimental_context


@pytest.mark.asyncio
@pytest.mark.parametrize("chatgpt", [False, True])
async def test_login_and_model_modifier_publish_exact_auth_facts_and_budget(chatgpt):
    storage = _FakeAuthStorage()
    async def login(callbacks):
        return {"access": "fixture-access", "refresh": "fixture-refresh", "expires": 4_102_444_800_000,
            **({"auth_mode": "chatgpt", "account_plan_type": "prolite"} if chatgpt else {})}
    def modify_models(models, credentials):
        assert models[0]["model_messages"] == messages("registered")
        return [{**models[0], "model_messages": messages("authenticated"), "supports_experimental_context": True}]
    runtime = ModelRuntime(provider_configs={})
    runtime._auth_storage = storage
    runtime.register_provider("owned", {"oauth": {"name": "Owned fixture", "login": login,
        "refresh": lambda credential: credential, "get_api_key": lambda credential: credential["access"], "modify_models": modify_models},
        "supports_codex_backend_routes": True, "requires_openai_auth": True,
        "models": [{"id": "m", "api": "openai-responses", "base_url": "https://fixture.invalid/v1", "model_messages": messages("registered")}]})
    await runtime.login_provider("owned", SimpleNamespace())
    selected = runtime.get_model("owned", "m")
    assert selected.model_messages == messages("authenticated") and selected.supports_experimental_context
    auth = runtime.token_budget_auth_context(selected)
    assert auth.auth_mode == ("chatgpt" if chatgpt else None)
    assert auth.account_plan_type == ("prolite" if chatgpt else None)
    assert auth.supports_codex_backend_routes and auth.requires_openai_auth
    assert not any((auth.has_env_key, auth.has_experimental_bearer_token, auth.has_auth, auth.has_aws))
    assert "fixture-access" not in str(asdict(auth))


@pytest.mark.parametrize("field", ["env_key", "experimental_bearer_token", "auth", "aws"])
def test_provider_restrictions_are_real_declared_facts(field):
    runtime = ModelRuntime(provider_configs={})
    runtime._auth_storage = _FakeAuthStorage()
    runtime._model_configs["owned"] = {field: "declared", "supports_codex_backend_routes": True, "requires_openai_auth": True}
    auth = runtime.token_budget_auth_context("owned")
    assert getattr(auth, "has_" + field)
    assert auth.auth_mode is None and auth.account_plan_type is None


def test_packaged_source_catalog_has_exact_disabled_activation_and_no_slug_guesses():
    metadata = get_provider_model_metadata({}, "gpt-6.1-sol")
    assert metadata["model_messages"] == source_context_metadata("gpt-6.1-sol")["model_messages"]
    assert metadata["model_messages"]["token_budget"]["enabled"] is False
    unknown = source_context_metadata("gpt-6.1-sol-invented")
    assert get_provider_model_metadata({}, "gpt-6.1-sol-invented")["model_messages"] == unknown["model_messages"]
    assert "token_budget" not in unknown["model_messages"]
    assert unknown["supports_experimental_context"] is False
    assert unknown["truncation_policy"] == {"mode": "bytes", "limit": 10000}


@pytest.mark.parametrize("bad", ["yes", 1, None])
def test_invalid_experimental_context_flag_is_not_guessed(bad):
    with pytest.raises(SettingsError):
        get_provider_model_metadata({"model_metadata": {"m": {"supports_experimental_context": bad}}}, "m")
    with pytest.raises(ProviderRegistrationError):
        ModelDefinition("p", "m", "m", "openai-responses", "https://fixture.invalid", supports_experimental_context=bad)
