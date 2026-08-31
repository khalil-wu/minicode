import json
import os

import pytest

from backend import config
from backend import config_helpers
from backend import config_providers


@pytest.fixture(autouse=True)
def _isolate_provider_credentials(monkeypatch):
    """Keep credential-scope tests independent from the developer machine."""

    exact_names = {
        "OPENAI_API_KEY",
        "CUSTOM_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENAI_BASE_URL",
        "CUSTOM_BASE_URL",
        "ANTHROPIC_BASE_URL",
        "OPENAI_PROXY_MODE",
        "CUSTOM_PROXY_MODE",
        "ANTHROPIC_PROXY_MODE",
        "MINICODE_OPENAI_IMAGE_BASE_URL",
        "MINICODE_CUSTOM_IMAGE_BASE_URL",
        "MINICODE_ANTHROPIC_IMAGE_BASE_URL",
    }
    scoped_prefixes = (
        "OPENAI_API_KEY_",
        "CUSTOM_API_KEY_",
        "ANTHROPIC_API_KEY_",
        "MINICODE_OPENAI_IMAGE_API_KEY_",
        "MINICODE_CUSTOM_IMAGE_API_KEY_",
        "MINICODE_ANTHROPIC_IMAGE_API_KEY_",
    )
    for name in list(os.environ):
        if name in exact_names or name.startswith(scoped_prefixes):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(config_helpers, "_RUNTIME_API_KEY_SCOPES", {})
    monkeypatch.setattr(config_helpers, "_RUNTIME_IMAGE_API_KEY_SCOPES", {})


def test_llm_settings_payload_supports_redacted_and_local_visible_views(monkeypatch, tmp_path):
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps({
            "llm": {
                "provider": "custom",
                "custom": {
                    "base_url": "https://api.deepseek.com/v1",
                    "model": "deepseek-v4-flash",
                    "available_models": ["deepseek-v4-flash"],
                    "wire_api": "chat",
                    "headers": {"X-Gateway": "desktop"},
                    "auth_header": True,
                },
                "provider_history": [{
                    "provider": "custom",
                    "provider_id": "openrouter",
                    "has_api_key": True,
                    "base_url": "https://openrouter.ai/api/v1",
                    "model": "anthropic/claude-sonnet-4",
                    "available_models": ["anthropic/claude-sonnet-4"],
                    "wire_api": "chat",
                    "headers": {"HTTP-Referer": "https://minicode.local"},
                    "auth_header": True,
                    "updated_at": 1,
                }],
            }
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_helpers, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(config_helpers, "_vault_api_key", lambda name: "")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    monkeypatch.setenv("CUSTOM_API_KEY", "test-custom-key")
    monkeypatch.setenv("CUSTOM_BASE_URL", "https://api.deepseek.com/v1")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    payload = config.get_llm_settings_payload()

    assert payload["openai"]["api_key"] == ""
    assert payload["openai"]["has_api_key"] is True
    assert payload["custom"]["api_key"] == ""
    assert payload["custom"]["has_api_key"] is True
    assert payload["custom"]["headers"] == {"X-Gateway": "desktop"}
    assert payload["custom"]["auth_header"] is True
    assert payload["provider_history"][0]["api_key"] == ""
    assert payload["provider_history"][0]["has_api_key"] is False
    assert payload["provider_history"][0]["headers"] == {
        "HTTP-Referer": "https://minicode.local"
    }
    assert payload["provider_history"][0]["auth_header"] is True

    local_payload = config.get_llm_settings_payload(include_api_keys=True)
    assert local_payload["openai"]["api_key"] == "test-openai-key"
    assert local_payload["custom"]["api_key"] == "test-custom-key"
    assert local_payload["openai"]["has_api_key"] is True
    assert local_payload["custom"]["has_api_key"] is True


def test_local_settings_surface_returns_the_endpoint_scoped_history_key(monkeypatch, tmp_path):
    from backend.services.llm_settings_service import get_llm_settings

    base_url = "https://www.supertoken.lol/v1"
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps({
            "llm": {
                "provider": "custom",
                "provider_history": [{
                    "provider": "custom",
                    "base_url": base_url,
                    "model": "deepseek-v4-flash",
                    "wire_api": "chat",
                    "updated_at": 1,
                }],
            }
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_helpers, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(config_helpers, "_vault_api_key", lambda _name: "")
    monkeypatch.setenv(config._scoped_vault_names("custom", base_url)[0], "sk-visible-endpoint-key")

    payload = get_llm_settings()

    assert payload["provider_history"][0]["has_api_key"] is True
    assert payload["provider_history"][0]["api_key"] == "sk-visible-endpoint-key"


def test_small_fast_model_round_trips_through_provider_settings(monkeypatch, tmp_path):
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps(
            {
                "llm": {
                    "provider": "custom",
                    "custom": {
                        "base_url": "https://gateway.example/v1",
                        "model": "main-model",
                        "small_fast_model": "fast-model",
                        "available_models": ["main-model", "fast-model"],
                        "wire_api": "responses",
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_helpers, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(config_helpers, "_vault_api_key", lambda name: "")
    monkeypatch.setenv("CUSTOM_API_KEY", "test-key")

    payload = config.get_llm_settings_payload()
    settings = config.load_llm_settings()

    assert payload["custom"]["small_fast_model"] == "fast-model"
    assert settings.small_fast_model == "fast-model"


def test_provider_proxy_mode_round_trips_through_settings_history_and_images(
    monkeypatch,
    tmp_path,
):
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps({"llm": {"provider": "custom"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_helpers, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(config_helpers, "_vault_api_key", lambda _name: "")
    monkeypatch.setattr(config_providers, "_set_runtime_api_key",
        lambda _provider, _api_key, _base_url="": None,
    )

    payload = config.save_llm_settings(
        {
            "custom": {
                "base_url": "https://gateway.example/v1",
                "model": "gateway-model",
                "available_models": ["gateway-model"],
                "wire_api": "chat",
                "proxy_mode": "direct",
                "image_mode": "inherit",
                "image_model": "image-model",
            }
        }
    )

    saved = json.loads(settings_file.read_text(encoding="utf-8"))
    assert payload["custom"]["proxy_mode"] == "direct"
    assert payload["provider_history"][0]["proxy_mode"] == "direct"
    assert saved["llm"]["custom"]["proxy_mode"] == "direct"
    assert saved["llm"]["provider_history"][0]["proxy_mode"] == "direct"
    assert config.get_custom_settings()["proxy_mode"] == "direct"
    assert config.get_image_generation_settings("custom")["proxy_mode"] == "direct"


def test_image_capability_uses_the_active_provider_model_catalog(monkeypatch, tmp_path):
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps({
            "llm": {
                "provider": "custom",
                "custom": {
                    "base_url": "https://gateway.example/v1",
                    "model": "text-model",
                    "available_models": ["text-model", "gpt-image-1"],
                    "wire_api": "chat",
                },
            },
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_helpers, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(config_helpers, "_vault_api_key", lambda _name: "")
    monkeypatch.setenv("CUSTOM_API_KEY", "local-key")

    image = config.get_image_generation_settings("custom")

    assert image["enabled"] is True
    assert image["base_url"] == "https://gateway.example/v1"
    assert image["api_key"] == "local-key"
    assert image["model"] == "gpt-image-1"


def test_image_capability_allows_keyless_endpoint_when_auth_header_is_disabled(
    monkeypatch,
    tmp_path,
):
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps({
            "llm": {
                "provider": "custom",
                "custom": {
                    "base_url": "http://127.0.0.1:11434/v1",
                    "model": "text-model",
                    "wire_api": "chat",
                    "image_mode": "custom",
                    "image_base_url": "http://127.0.0.1:11434/v1",
                    "image_model": "local-image-model",
                    "auth_header": False,
                    "headers": {"X-Local-Runtime": "enabled"},
                },
            },
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_helpers, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(config_helpers, "_vault_api_key", lambda _name: "")

    image = config.get_image_generation_settings("custom")

    assert image["enabled"] is True
    assert image["api_key"] == ""
    assert image["auth_header"] is False
    assert image["default_headers"] == (("X-Local-Runtime", "enabled"),)


def test_image_capability_requires_key_only_for_explicit_auth_header(
    monkeypatch,
    tmp_path,
):
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps({
            "llm": {
                "provider": "custom",
                "custom": {
                    "base_url": "https://gateway.example/v1",
                    "model": "text-model",
                    "wire_api": "chat",
                    "image_mode": "custom",
                    "image_base_url": "https://images.example/v1",
                    "image_model": "image-model",
                    "auth_header": True,
                },
            },
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_helpers, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(config_helpers, "_vault_api_key", lambda _name: "")

    image = config.get_image_generation_settings("custom")

    assert image["enabled"] is False
    assert image["reason"] == "auth_header=true requires an image API key."


def test_invalid_provider_proxy_mode_falls_back_to_inherit(monkeypatch, tmp_path):
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps({"llm": {"provider": "custom"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_helpers, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(config_helpers, "_vault_api_key", lambda _name: "")

    payload = config.save_llm_settings(
        {
            "custom": {
                "base_url": "https://gateway.example/v1",
                "model": "gateway-model",
                "wire_api": "chat",
                "proxy_mode": "automatic-fallback",
            }
        }
    )

    assert payload["custom"]["proxy_mode"] == "inherit"
    assert payload["provider_history"][0]["proxy_mode"] == "inherit"


def test_proxy_mode_is_projected_for_every_builtin_provider(monkeypatch, tmp_path):
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps(
            {
                "llm": {
                    "provider": "openai",
                    "openai": {"proxy_mode": "direct"},
                    "anthropic": {"proxy_mode": "direct"},
                    "custom": {"proxy_mode": "direct"},
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_helpers, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(config_helpers, "_vault_api_key", lambda _name: "")

    payload = config.get_llm_settings_payload()

    assert payload["openai"]["proxy_mode"] == "direct"
    assert payload["anthropic"]["proxy_mode"] == "direct"
    assert payload["custom"]["proxy_mode"] == "direct"


def test_custom_provider_transport_can_be_configured_entirely_from_custom_env(monkeypatch):
    monkeypatch.setattr(config_helpers, "_vault_api_key", lambda _name: "")
    monkeypatch.setenv("CUSTOM_API_KEY", "custom-runtime-key")
    monkeypatch.setenv("CUSTOM_BASE_URL", "https://gateway.example/v1")
    monkeypatch.setenv("CUSTOM_MODEL", "vendor-model")
    monkeypatch.setenv("CUSTOM_WIRE_API", "responses")
    monkeypatch.setenv("CUSTOM_REASONING_EFFORT", "high")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://wrong-openai.example/v1")
    monkeypatch.setenv("OPENAI_MODEL", "wrong-openai-model")
    monkeypatch.setenv("OPENAI_WIRE_API", "chat")

    settings = config.get_custom_settings({"llm": {"custom": {}}})

    assert settings["api_key"] == "custom-runtime-key"
    assert settings["base_url"] == "https://gateway.example/v1"
    assert settings["model"] == "vendor-model"
    assert settings["wire_api"] == "responses"
    assert settings["configured_reasoning_effort"] == "high"


def test_explicit_custom_provider_settings_override_custom_and_openai_env(monkeypatch):
    monkeypatch.setattr(config_helpers, "_vault_api_key", lambda _name: "")
    monkeypatch.setenv("CUSTOM_BASE_URL", "https://env-custom.example/v1")
    monkeypatch.setenv("CUSTOM_MODEL", "env-custom-model")
    monkeypatch.setenv("CUSTOM_WIRE_API", "responses")
    monkeypatch.setenv("CUSTOM_REASONING_EFFORT", "low")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://env-openai.example/v1")
    monkeypatch.setenv("OPENAI_MODEL", "env-openai-model")
    monkeypatch.setenv("OPENAI_WIRE_API", "responses")

    settings = config.get_custom_settings(
        {
            "llm": {
                "custom": {
                    "base_url": "https://saved-custom.example/v1",
                    "model": "saved-custom-model",
                    "wire_api": "chat",
                    "reasoning_effort": "medium",
                }
            }
        }
    )

    assert settings["base_url"] == "https://saved-custom.example/v1"
    assert settings["model"] == "saved-custom-model"
    assert settings["wire_api"] == "chat"
    assert settings["configured_reasoning_effort"] == "medium"


def test_invalid_custom_wire_api_env_is_rejected(monkeypatch):
    """A typo in CUSTOM_WIRE_API must not silently switch protocols.

    Falling back to "chat" sent the user's requests over a different wire
    contract than they configured, and made the fail-closed check in
    build_wire_adapter unreachable for real misconfiguration.
    """
    monkeypatch.setattr(config_helpers, "_vault_api_key", lambda _name: "")
    monkeypatch.setenv("CUSTOM_WIRE_API", "not-a-wire-contract")

    with pytest.raises(config.SettingsError, match="Unknown wire API"):
        config.get_custom_settings({"llm": {"custom": {}}})


def test_custom_responses_optional_features_use_only_custom_env(monkeypatch):
    monkeypatch.setattr(config_helpers, "_vault_api_key", lambda _name: "")
    monkeypatch.setenv("OPENAI_RESPONSES_STATEFUL", "true")
    monkeypatch.setenv("OPENAI_PROMPT_CACHE_RETENTION", "24h")
    monkeypatch.delenv("CUSTOM_RESPONSES_STATEFUL", raising=False)
    monkeypatch.delenv("CUSTOM_PROMPT_CACHE_RETENTION", raising=False)
    settings_data = {
        "llm": {
            "custom": {
                "base_url": "https://gateway.example/v1",
                "model": "vendor-model",
                "wire_api": "responses",
            }
        }
    }

    isolated = config.get_custom_settings(settings_data)

    assert "responses_stateful_continuation" not in isolated
    assert isolated["prompt_cache_retention"] == ""

    monkeypatch.setenv("CUSTOM_RESPONSES_STATEFUL", "true")
    monkeypatch.setenv("CUSTOM_PROMPT_CACHE_RETENTION", "in_memory")
    configured = config.get_custom_settings(settings_data)

    assert "responses_stateful_continuation" not in configured
    assert configured["prompt_cache_retention"] == "in_memory"


def test_openai_responses_optional_features_do_not_reuse_custom_env(monkeypatch):
    monkeypatch.setattr(config_helpers, "_vault_api_key", lambda _name: "")
    monkeypatch.delenv("OPENAI_RESPONSES_STATEFUL", raising=False)
    monkeypatch.delenv("OPENAI_PROMPT_CACHE_RETENTION", raising=False)
    monkeypatch.setenv("CUSTOM_RESPONSES_STATEFUL", "true")
    monkeypatch.setenv("CUSTOM_PROMPT_CACHE_RETENTION", "24h")

    settings = config.get_openai_settings(
        {
            "llm": {
                "openai": {
                    "base_url": "https://api.openai.com/v1",
                    "model": "gpt-5.5",
                    "wire_api": "responses",
                }
            }
        }
    )

    assert "responses_stateful_continuation" not in settings
    assert settings["prompt_cache_retention"] == ""


def test_model_metadata_normalizes_claude_and_codex_catalog_fields() -> None:
    metadata = config._coerce_model_metadata(
        {
            "claude-capability-shape": {
                "max_input_tokens": 200_000,
            },
            "codex-core-shape": {
                "supported_reasoning_levels": [
                    {"effort": "low", "description": "Low"},
                    {"effort": "ultra", "description": "Ultra"},
                    {"effort": "focused", "description": "Focused"},
                ],
            },
            "codex-app-server-shape": {
                "supportedReasoningEfforts": [
                    {"reasoningEffort": "minimal", "description": "Minimal"},
                    {"reasoningEffort": "xhigh", "description": "Extra high"},
                ],
            },
        }
    )

    assert metadata == {
        "claude-capability-shape": {
            "context_window": 200_000,
            "source": "provider",
        },
        "codex-core-shape": {
            "reasoning_effort_levels": ["low", "ultra", "focused"],
            "source": "provider",
        },
        "codex-app-server-shape": {
            "reasoning_effort_levels": ["minimal", "xhigh"],
            "source": "provider",
        },
    }


def test_scoped_saved_key_wins_for_current_base_url(monkeypatch):
    openai_base_url = "https://api.openai.com/v1"
    custom_base_url = "https://api.deepseek.com/v1"
    openai_scoped = config._scoped_vault_name("openai", openai_base_url)
    custom_scoped = config._scoped_vault_name("custom", custom_base_url)

    def fake_vault(name: str) -> str:
        return {
            openai_scoped: "saved-openai-key",
            custom_scoped: "saved-custom-key",
            "OPENAI_API_KEY": "global-openai-key",
            "CUSTOM_API_KEY": "global-custom-key",
        }.get(name, "")

    monkeypatch.setattr(config_helpers, "_vault_api_key", fake_vault)
    monkeypatch.setenv("OPENAI_API_KEY", "runtime-openai-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://lucen.cc/v1")
    monkeypatch.setenv("CUSTOM_API_KEY", "runtime-custom-key")
    monkeypatch.setenv("CUSTOM_BASE_URL", "https://api.deepseek.com/v1")

    assert config.resolve_provider_api_key_for_base_url("openai", openai_base_url) == "saved-openai-key"
    assert config.resolve_provider_api_key_for_base_url("custom", custom_base_url) == "saved-custom-key"


def test_runtime_env_key_is_used_when_no_scoped_key_exists(monkeypatch):
    def fake_vault(name: str) -> str:
        return {
            "OPENAI_API_KEY": "global-openai-key",
            "CUSTOM_API_KEY": "global-custom-key",
        }.get(name, "")

    monkeypatch.setattr(config_helpers, "_vault_api_key", fake_vault)
    monkeypatch.setenv("OPENAI_API_KEY", "runtime-openai-key")
    monkeypatch.setenv("CUSTOM_API_KEY", "runtime-custom-key")

    assert config.resolve_provider_api_key_for_base_url("openai", "https://lucen.cc/v1") == "runtime-openai-key"
    assert config.resolve_provider_api_key_for_base_url("custom", "https://api.deepseek.com/v1") == "runtime-custom-key"


def test_custom_provider_default_key_lookup_uses_custom_base_url_scope(monkeypatch):
    custom_base_url = "https://custom-gateway.example/v1"
    openai_base_url = "https://openai-gateway.example/v1"
    custom_scoped = config._scoped_vault_name("custom", custom_base_url)
    wrong_scoped = config._scoped_vault_name("custom", openai_base_url)

    def fake_vault(name: str) -> str:
        return {
            custom_scoped: "saved-custom-endpoint-key",
            wrong_scoped: "wrong-openai-endpoint-key",
        }.get(name, "")

    monkeypatch.setattr(config_helpers, "_vault_api_key", fake_vault)
    monkeypatch.delenv("CUSTOM_API_KEY", raising=False)
    monkeypatch.setenv("CUSTOM_BASE_URL", custom_base_url)
    monkeypatch.setenv("OPENAI_BASE_URL", openai_base_url)

    assert config._provider_api_key("custom") == "saved-custom-endpoint-key"


def test_custom_provider_does_not_reuse_openai_scoped_key(monkeypatch):
    base_url = "https://lucen.cc/v1"
    openai_scoped = config._scoped_vault_name("openai", base_url)

    def fake_vault(name: str) -> str:
        return {
            openai_scoped: "saved-openai-lucen-key",
            "OPENAI_API_KEY": "global-openai-key",
        }.get(name, "")

    monkeypatch.setattr(config_helpers, "_vault_api_key", fake_vault)
    monkeypatch.delenv("CUSTOM_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "runtime-openai-key")
    monkeypatch.setenv("OPENAI_BASE_URL", base_url)

    assert config.resolve_provider_api_key_for_base_url("custom", base_url) == ""


def test_custom_provider_key_scope_uses_the_exact_endpoint(monkeypatch):
    current_base_url = "https://bbe.to/v1"
    previous_base_url = "https://api.bbe.to/v1"
    current_scoped = config._scoped_vault_name("custom", current_base_url)
    previous_scoped = config._scoped_vault_name("custom", previous_base_url)

    def fake_vault(name: str) -> str:
        return {
            current_scoped: "••••",
            previous_scoped: "sk-full-bbe-key",
            "CUSTOM_API_KEY": "••••",
        }.get(name, "")

    monkeypatch.setattr(config_helpers, "_vault_api_key", fake_vault)
    monkeypatch.setenv("CUSTOM_API_KEY", "••••")

    assert config.resolve_provider_api_key_for_base_url("custom", current_base_url) == ""


def test_custom_provider_does_not_reuse_a_different_endpoint_scope(monkeypatch):
    messages_base_url = "https://api.deepseek.com/anthropic"
    chat_base_url = "https://api.deepseek.com/v1"
    messages_scoped = config._scoped_vault_name("custom", messages_base_url)
    chat_scoped = config._scoped_vault_name("custom", chat_base_url)

    def fake_vault(name: str) -> str:
        return {
            messages_scoped: "••••",
            chat_scoped: "sk-full-deepseek-key",
            "CUSTOM_API_KEY": "sk-unrelated-global-key",
        }.get(name, "")

    monkeypatch.setattr(config_helpers, "_vault_api_key", fake_vault)
    monkeypatch.delenv("CUSTOM_API_KEY", raising=False)
    monkeypatch.setenv("CUSTOM_BASE_URL", chat_base_url)

    assert config.resolve_provider_api_key_for_base_url("custom", messages_base_url) == ""


def test_save_llm_settings_keeps_key_for_empty_or_shortened_submissions(monkeypatch, tmp_path):
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps({
            "llm": {
                "provider": "openai",
                "openai": {
                    "base_url": "https://api.openai.com/v1",
                    "model": "gpt-5",
                },
                "custom": {
                    "base_url": "https://api.deepseek.com/v1",
                    "model": "deepseek-v4-flash",
                    "wire_api": "chat",
                },
            }
        }),
        encoding="utf-8",
    )
    calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(config_helpers, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(config_providers, "_set_runtime_api_key",
        lambda provider, api_key, base_url="": calls.append((provider, api_key, base_url)),
    )

    for value in ("", "••••", "tes…-key"):
        config.save_llm_settings({
            "provider": "custom",
            "custom": {
                "api_key": value,
                "base_url": "https://api.deepseek.com/v1",
                "model": "deepseek-v4-flash",
                "wire_api": "chat",
            },
        })

    assert calls == []


def test_save_llm_settings_replaces_key_only_with_full_user_value(monkeypatch, tmp_path):
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(json.dumps({"llm": {"provider": "custom"}}), encoding="utf-8")
    calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(config_helpers, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(config_helpers, "_vault_api_key", lambda name: "")
    monkeypatch.setattr(config_providers, "_set_runtime_api_key",
        lambda provider, api_key, base_url="": calls.append((provider, api_key, base_url)),
    )

    config.save_llm_settings({
        "provider": "custom",
        "custom": {
            "api_key": "sk-new-full-value",
            "base_url": "https://gateway.example/v1",
            "model": "model-1",
            "wire_api": "chat",
        },
    })

    assert calls == [("custom", "sk-new-full-value", "https://gateway.example/v1")]


def test_openai_slot_preserves_the_explicit_responses_transport(monkeypatch, tmp_path):
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(json.dumps({"llm": {"provider": "openai"}}), encoding="utf-8")
    monkeypatch.setattr(config_helpers, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(config_helpers, "_vault_api_key", lambda name: "")

    payload = config.save_llm_settings({
        "provider": "openai",
        "openai": {
            "base_url": "https://api.deepseek.com/v1",
            "model": "deepseek-v4-flash",
            "wire_api": "responses",
            "responses_stateful_continuation": True,
            "prompt_cache_retention": "24h",
        },
    })

    assert payload["openai"]["wire_api"] == "responses"
    assert "responses_stateful_continuation" not in payload["openai"]
    assert payload["openai"]["prompt_cache_retention"] == "24h"


def test_deepseek_anthropic_endpoint_keeps_messages_transport(monkeypatch, tmp_path):
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(json.dumps({"llm": {"provider": "custom"}}), encoding="utf-8")
    monkeypatch.setattr(config_helpers, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(config_helpers, "_vault_api_key", lambda name: "")

    payload = config.save_llm_settings({
        "provider": "custom",
        "custom": {
            "base_url": "https://api.deepseek.com/anthropic",
            "model": "deepseek-v4-pro",
            "wire_api": "anthropic",
        },
    })

    assert payload["custom"]["wire_api"] == "anthropic"
    assert payload["custom"]["base_url"] == "https://api.deepseek.com/anthropic"
    assert payload["custom"]["model"] == "deepseek-v4-pro"
    assert "claude-sonnet-4-6" not in payload["custom"]["available_models"]


def test_custom_anthropic_preserves_the_configured_model_id(monkeypatch, tmp_path):
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps({
            "llm": {
                "provider": "custom",
                "custom": {
                    "base_url": "https://api.deepseek.com/anthropic",
                    "model": "claude-sonnet-4-6",
                    "available_models": ["claude-sonnet-4-6"],
                    "wire_api": "anthropic",
                },
            },
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_helpers, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(config_helpers, "_vault_api_key", lambda name: "")

    custom = config.get_custom_settings()

    assert custom["wire_api"] == "anthropic"
    assert custom["model"] == "claude-sonnet-4-6"
    assert custom["available_models"] == ["claude-sonnet-4-6"]


def test_provider_history_keeps_explicit_provider_profiles_distinct(monkeypatch, tmp_path):
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps({
            "llm": {
                "provider": "custom",
                "provider_history": [
                    {
                        "provider": "custom",
                        "provider_id": "custom_anthropic",
                        "base_url": "https://api.deepseek.com/anthropic",
                        "model": "claude-sonnet-4-6",
                        "available_models": ["claude-sonnet-4-6"],
                        "wire_api": "anthropic",
                        "updated_at": 2,
                    },
                    {
                        "provider": "openai",
                        "provider_id": "deepseek",
                        "base_url": "https://api.deepseek.com/v1",
                        "model": "deepseek-v4-flash",
                        "available_models": ["deepseek-v4-flash"],
                        "wire_api": "chat",
                        "updated_at": 1,
                    },
                ],
            },
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_helpers, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(config_helpers, "_vault_api_key", lambda name: "")

    history = config.get_llm_settings_payload()["provider_history"]

    assert len(history) == 2
    assert history[0]["provider_id"] == "custom_anthropic"
    assert history[0]["wire_api"] == "anthropic"
    assert history[0]["model"] == "claude-sonnet-4-6"
    assert history[1]["provider_id"] == "openai"
    assert history[1]["wire_api"] == "chat"


def test_provider_history_keeps_provider_and_transport_identity(monkeypatch, tmp_path):
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps({
            "llm": {
                "provider": "custom",
                "provider_history": [
                    {
                        "provider": "custom",
                        "base_url": "https://gateway.example/v1",
                        "model": "new-model",
                        "wire_api": "responses",
                        "updated_at": 2,
                        "api_key": "must-not-be-persisted",
                    },
                    {
                        "provider": "openai",
                        "base_url": "https://gateway.example/v1/",
                        "model": "old-model",
                        "wire_api": "chat",
                        "updated_at": 1,
                    },
                ],
            }
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_helpers, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(config_helpers, "_vault_api_key", lambda name: "")

    history = config.get_llm_settings_payload()["provider_history"]

    assert len(history) == 2
    assert history[0]["model"] == "new-model"
    assert history[0]["wire_api"] == "responses"
    assert history[0]["api_key"] == ""


def test_provider_history_uses_one_profile_per_provider_endpoint(monkeypatch, tmp_path):
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps({
            "llm": {
                "provider": "custom",
                "provider_history": [
                    {
                        "provider": "custom",
                        "base_url": "https://gateway.example/v1",
                        "model": "responses-model",
                        "wire_api": "responses",
                        "updated_at": 20,
                    },
                    {
                        "provider": "custom",
                        "base_url": "https://gateway.example/v1/",
                        "model": "chat-model",
                        "wire_api": "chat",
                        "updated_at": 10,
                    },
                ],
            }
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_helpers, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(config_helpers, "_vault_api_key", lambda name: "")

    history = config.get_llm_settings_payload()["provider_history"]

    assert len(history) == 1
    assert history[0]["model"] == "responses-model"
    assert history[0]["wire_api"] == "responses"


def test_provider_history_does_not_hide_entries_by_hostname_or_model(monkeypatch, tmp_path):
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps({
            "llm": {
                "provider": "openai",
                "provider_history": [{
                    "provider": "anthropic",
                    "provider_id": "anthropic_off",
                    "base_url": "http://127.0.0.1:15721",
                    "model": "claude-sonnet-4-6",
                    "wire_api": "anthropic",
                    "updated_at": 1,
                }],
            }
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_helpers, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(config_helpers, "_vault_api_key", lambda name: "")

    history = config.get_llm_settings_payload()["provider_history"]
    assert len(history) == 1
    assert history[0]["provider_id"] == "anthropic"


def test_targeted_provider_save_does_not_materialize_runtime_only_providers(
    monkeypatch,
    tmp_path,
):
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps({
            "llm": {
                "provider": "custom",
                "custom": {
                    "display_name": "Work gateway",
                    "base_url": "https://gateway.example/v1",
                    "model": "vendor-model-1",
                    "available_models": ["vendor-model-1"],
                    "wire_api": "responses",
                },
                "provider_history": [],
            }
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_helpers, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(config_helpers, "_vault_api_key", lambda name: "")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:15721")
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")

    payload = config.save_llm_settings({
        "custom": {
            "display_name": "Work gateway",
            "base_url": "https://gateway.example/v1",
            "model": "vendor-model-2",
            "available_models": ["vendor-model-2", "vendor-model-1"],
            "wire_api": "responses",
        },
    })

    saved = json.loads(settings_file.read_text(encoding="utf-8"))
    assert "anthropic" not in saved["llm"]
    assert saved["llm"]["custom"]["model"] == "vendor-model-2"
    assert [entry["provider"] for entry in saved["llm"]["provider_history"]] == ["custom"]
    assert payload["anthropic"]["base_url"] == "http://127.0.0.1:15721"
    assert all(
        entry["base_url"] != "http://127.0.0.1:15721"
        for entry in payload["provider_history"]
    )


def test_provider_display_name_is_preserved_in_payload_and_history(monkeypatch, tmp_path):
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps({"llm": {"provider": "openai"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_helpers, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(config_providers, "_set_runtime_api_key", lambda provider, api_key, base_url="": None)
    monkeypatch.setattr(config_helpers, "_vault_api_key", lambda name: "")

    payload = config.save_llm_settings({
        "custom": {
            "display_name": "api.bbe.to",
            "base_url": "https://api.bbe.to/v1",
            "model": "gpt-5",
            "available_models": ["gpt-5"],
            "wire_api": "responses",
        },
    })

    assert payload["provider"] == "openai"
    assert payload["custom"]["display_name"] == "api.bbe.to"
    assert payload["provider_history"][0]["display_name"] == "api.bbe.to"
    assert "responses_stateful_continuation" not in payload["provider_history"][0]
    assert payload["provider_history"][0]["prompt_cache_retention"] == ""

    saved = json.loads(settings_file.read_text(encoding="utf-8"))
    assert saved["llm"]["provider"] == "openai"
    assert saved["llm"]["custom"]["display_name"] == "api.bbe.to"
    assert saved["llm"]["provider_history"][0]["display_name"] == "api.bbe.to"
    assert "responses_stateful_continuation" not in saved["llm"]["provider_history"][0]
    assert saved["llm"]["provider_history"][0]["prompt_cache_retention"] == ""


def test_save_custom_settings_switching_to_responses_keeps_optional_features_off(monkeypatch, tmp_path):
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps(
            {
                "llm": {
                    "provider": "custom",
                    "custom": {
                        "base_url": "https://gateway.example/v1",
                        "model": "gpt-5.5",
                        "wire_api": "chat",
                        "responses_stateful_continuation": False,
                        "prompt_cache_retention": "",
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_helpers, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(config_providers, "_set_runtime_api_key", lambda provider, api_key, base_url="": None)
    monkeypatch.setattr(config_helpers, "_vault_api_key", lambda name: "")
    monkeypatch.delenv("OPENAI_RESPONSES_STATEFUL", raising=False)
    monkeypatch.delenv("OPENAI_PROMPT_CACHE_RETENTION", raising=False)

    payload = config.save_llm_settings({
        "provider": "custom",
        "custom": {
            "base_url": "https://gateway.example/v1",
            "model": "gpt-5.5",
            "wire_api": "responses",
        },
    })

    assert payload["custom"]["wire_api"] == "responses"
    assert "responses_stateful_continuation" not in payload["custom"]
    assert payload["custom"]["prompt_cache_retention"] == ""


def test_save_custom_settings_switching_to_responses_uses_custom_optional_env(monkeypatch, tmp_path):
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps(
            {
                "llm": {
                    "provider": "custom",
                    "custom": {
                        "base_url": "https://gateway.example/v1",
                        "model": "gpt-5.5",
                        "wire_api": "chat",
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_helpers, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(config_providers, "_set_runtime_api_key", lambda provider, api_key, base_url="": None)
    monkeypatch.setattr(config_helpers, "_vault_api_key", lambda _name: "")
    monkeypatch.setenv("OPENAI_RESPONSES_STATEFUL", "false")
    monkeypatch.setenv("OPENAI_PROMPT_CACHE_RETENTION", "off")
    monkeypatch.setenv("CUSTOM_RESPONSES_STATEFUL", "true")
    monkeypatch.setenv("CUSTOM_PROMPT_CACHE_RETENTION", "24h")

    payload = config.save_llm_settings(
        {
            "provider": "custom",
            "custom": {
                "base_url": "https://gateway.example/v1",
                "model": "gpt-5.5",
                "wire_api": "responses",
            },
        }
    )

    assert "responses_stateful_continuation" not in payload["custom"]
    assert payload["custom"]["prompt_cache_retention"] == "24h"


def test_save_custom_settings_preserves_explicit_responses_cache_disable(monkeypatch, tmp_path):
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps(
            {
                "llm": {
                    "provider": "custom",
                    "custom": {
                        "base_url": "https://gateway.example/v1",
                        "model": "gpt-5.5",
                        "wire_api": "responses",
                        "responses_stateful_continuation": False,
                        "prompt_cache_retention": "",
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_helpers, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(config_providers, "_set_runtime_api_key", lambda provider, api_key, base_url="": None)
    monkeypatch.setattr(config_helpers, "_vault_api_key", lambda name: "")
    monkeypatch.delenv("OPENAI_RESPONSES_STATEFUL", raising=False)
    monkeypatch.delenv("OPENAI_PROMPT_CACHE_RETENTION", raising=False)

    payload = config.save_llm_settings({
        "provider": "custom",
        "custom": {
            "base_url": "https://gateway.example/v1",
            "model": "gpt-5.5",
            "wire_api": "responses",
        },
    })

    assert "responses_stateful_continuation" not in payload["custom"]
    assert payload["custom"]["prompt_cache_retention"] == ""


def test_delete_llm_provider_history_removes_entry_and_scoped_key(monkeypatch, tmp_path):
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps({
            "llm": {
                "provider": "custom",
                "custom": {
                    "base_url": "https://api.deepseek.com/v1",
                    "model": "deepseek-v4-flash",
                    "available_models": ["deepseek-v4-flash"],
                    "wire_api": "chat",
                },
                "provider_history": [
                    {
                        "provider": "custom",
                        "provider_id": "openrouter",
                        "base_url": "https://openrouter.ai/api/v1",
                        "model": "anthropic/claude-sonnet-4",
                        "available_models": ["anthropic/claude-sonnet-4"],
                        "wire_api": "chat",
                        "updated_at": 2,
                    },
                    {
                        "provider": "custom",
                        "provider_id": "deepseek",
                        "base_url": "https://api.deepseek.com/v1",
                        "model": "deepseek-v4-flash",
                        "available_models": ["deepseek-v4-flash"],
                        "wire_api": "chat",
                        "updated_at": 1,
                    },
                ],
            }
        }),
        encoding="utf-8",
    )
    cleared: list[tuple[str, str]] = []
    monkeypatch.setattr(config_helpers, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(
        config,
        "_clear_scoped_runtime_api_key",
        lambda provider, base_url="": cleared.append((provider, base_url)),
    )

    payload = config.delete_llm_provider_history({
        "provider": "custom",
        "provider_id": "openrouter",
        "base_url": "https://openrouter.ai/api/v1",
        "model": "anthropic/claude-sonnet-4",
        "wire_api": "chat",
    })

    assert [entry["provider_id"] for entry in payload["provider_history"]] == ["custom"]
    assert cleared == [("custom", "https://openrouter.ai/api/v1")]


def test_get_custom_settings_preserves_the_configured_model_list(monkeypatch, tmp_path):
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps({
            "llm": {
                "provider": "custom",
                "custom": {
                    "base_url": "https://api.deepseek.com/v1",
                    "model": "deepseek-v4-pro",
                    "available_models": [
                        "deepseek-v4-pro",
                        "gpt-5",
                        "grok-3",
                        "deepseek-v4-flash",
                        "deepseek-chat",
                    ],
                    "wire_api": "chat",
                },
            }
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_helpers, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(config_helpers, "_vault_api_key", lambda name: "")
    monkeypatch.delenv("CUSTOM_API_KEY", raising=False)

    payload = config.get_custom_settings()

    assert payload["available_models"] == [
        "deepseek-v4-pro",
        "gpt-5",
        "grok-3",
        "deepseek-v4-flash",
        "deepseek-chat",
    ]


def test_get_openai_settings_does_not_invent_reasoning_or_legacy_stateful_defaults(monkeypatch, tmp_path):
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps(
            {
                "llm": {
                    "provider": "openai",
                    "openai": {
                        "base_url": "https://api.openai.com/v1",
                        "model": "gpt-5.5",
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_helpers, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(config_helpers, "_vault_api_key", lambda name: "")
    monkeypatch.delenv("OPENAI_REASONING_EFFORT", raising=False)
    monkeypatch.delenv("OPENAI_RESPONSES_STATEFUL", raising=False)

    payload = config.get_openai_settings()

    assert payload["reasoning_effort"] == ""
    assert payload["responses_reasoning_summary"] == "off"
    assert payload["wire_api"] == "responses"
    assert "responses_stateful_continuation" not in payload
    assert payload["prompt_cache_retention"] == ""


def test_load_llm_settings_marks_official_anthropic_wire_api(monkeypatch, tmp_path):
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps(
            {
                "llm": {
                    "provider": "anthropic",
                    "anthropic": {
                        "base_url": "https://api.anthropic.com",
                        "model": "claude-sonnet-4-6",
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_helpers, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(config_helpers, "_vault_api_key", lambda name: "test-anthropic-key")

    settings = config.load_llm_settings()

    assert settings.wire_api == "anthropic"
    assert not hasattr(settings, "responses_stateful_continuation")
    assert settings.prompt_cache_retention == ""


def test_get_custom_settings_does_not_invent_responses_optional_features(monkeypatch, tmp_path):
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps(
            {
                "llm": {
                    "provider": "custom",
                    "custom": {
                        "base_url": "https://example-gateway.test/v1",
                        "model": "gpt-5.5",
                        "wire_api": "responses",
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_helpers, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(config_helpers, "_vault_api_key", lambda name: "")
    monkeypatch.delenv("OPENAI_REASONING_EFFORT", raising=False)
    monkeypatch.delenv("OPENAI_RESPONSES_STATEFUL", raising=False)

    payload = config.get_custom_settings()

    assert payload["reasoning_effort"] == ""
    assert payload["responses_reasoning_summary"] == "off"
    assert "responses_stateful_continuation" not in payload
    assert payload["prompt_cache_retention"] == ""


def test_get_custom_settings_keeps_conservative_defaults_for_chat_gateways(monkeypatch, tmp_path):
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps(
            {
                "llm": {
                    "provider": "custom",
                    "custom": {
                        "base_url": "https://example-gateway.test/v1",
                        "model": "gpt-5.5",
                        "wire_api": "chat",
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_helpers, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(config_helpers, "_vault_api_key", lambda name: "")
    monkeypatch.delenv("OPENAI_RESPONSES_STATEFUL", raising=False)
    monkeypatch.delenv("OPENAI_PROMPT_CACHE_RETENTION", raising=False)

    payload = config.get_custom_settings()

    assert payload["wire_api"] == "chat"
    assert "responses_stateful_continuation" not in payload
    assert payload["prompt_cache_retention"] == ""


def test_get_custom_settings_preserves_explicit_responses_for_any_endpoint(monkeypatch, tmp_path):
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps(
            {
                "llm": {
                    "provider": "custom",
                    "custom": {
                        "base_url": "https://api.deepseek.com/v1",
                        "model": "deepseek-v4-flash",
                        "wire_api": "responses",
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_helpers, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(config_helpers, "_vault_api_key", lambda name: "")
    monkeypatch.delenv("OPENAI_RESPONSES_STATEFUL", raising=False)
    monkeypatch.delenv("OPENAI_PROMPT_CACHE_RETENTION", raising=False)

    payload = config.get_custom_settings()

    assert payload["wire_api"] == "responses"
    assert "responses_stateful_continuation" not in payload
    assert payload["prompt_cache_retention"] == ""


def test_get_custom_settings_respects_explicit_responses_cache_disable(monkeypatch, tmp_path):
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps(
            {
                "llm": {
                    "provider": "custom",
                    "custom": {
                        "base_url": "https://example-gateway.test/v1",
                        "model": "gpt-5.5",
                        "wire_api": "responses",
                        "responses_stateful_continuation": False,
                        "prompt_cache_retention": "off",
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_helpers, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(config_helpers, "_vault_api_key", lambda name: "")
    monkeypatch.delenv("OPENAI_RESPONSES_STATEFUL", raising=False)
    monkeypatch.delenv("OPENAI_PROMPT_CACHE_RETENTION", raising=False)

    payload = config.get_custom_settings()

    assert payload["wire_api"] == "responses"
    assert "responses_stateful_continuation" not in payload
    assert payload["prompt_cache_retention"] == ""


def test_model_metadata_is_retained_only_for_the_same_provider_target(monkeypatch, tmp_path):
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps(
            {
                "llm": {
                    "provider": "custom",
                    "custom": {
                        "base_url": "https://gateway.example/v1",
                        "model": "model-a",
                        "wire_api": "responses",
                        "model_metadata": {
                            "model-a": {
                                "context_window": 128_000,
                                "reasoning_effort_levels": ["low", "high"],
                            }
                        },
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_helpers, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(config_helpers, "_vault_api_key", lambda _name: "")

    same_target = config.save_llm_settings(
        {
            "custom": {
                "base_url": "https://gateway.example/v1",
                "model": "model-a",
                "wire_api": "responses",
            }
        }
    )
    changed_target = config.save_llm_settings(
        {
            "custom": {
                "base_url": "https://gateway.example/v1",
                "model": "model-b",
                "wire_api": "responses",
            }
        }
    )

    assert same_target["custom"]["model_metadata"]["model-a"]["context_window"] == 128_000
    assert same_target["custom"]["reasoning_effort_levels"] == ["low", "high"]
    assert changed_target["custom"]["model_metadata"] == {}
    assert changed_target["custom"]["reasoning_effort_levels"] == []
    assert changed_target["custom"]["context_window_source"] == "fallback"
    assert changed_target["custom"]["context_window_verified"] is False


def test_provider_model_metadata_drives_loaded_context_and_effective_reasoning(
    monkeypatch,
    tmp_path,
):
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps(
            {
                "llm": {
                    "provider": "custom",
                    "custom": {
                        "base_url": "https://gateway.example/v1",
                        "model": "provider-model",
                        "wire_api": "responses",
                        "reasoning_effort": "high",
                        "model_metadata": {
                            "provider-model": {
                                "context_window": 96_000,
                                "reasoning_effort_levels": ["low", "high"],
                            }
                        },
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_helpers, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(config_helpers, "_vault_api_key", lambda _name: "test-key")

    payload = config.get_llm_settings_payload()
    settings = config.load_llm_settings()
    app_config = config.load_config()

    assert payload["custom"]["configured_reasoning_effort"] == "high"
    assert payload["custom"]["effective_reasoning_effort"] == "high"
    assert payload["custom"]["reasoning_effort_supported"] is True
    assert payload["custom"]["context_window"] == 96_000
    assert payload["custom"]["context_window_source"] == "provider"
    assert payload["custom"]["context_window_verified"] is True
    assert settings.context_window == 96_000
    assert settings.context_window_source == "provider"
    assert settings.context_window_verified is True
    assert app_config.token_budget.total == 96_000


def test_configured_reasoning_preference_is_not_reported_as_effective_without_metadata(
    monkeypatch,
    tmp_path,
):
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps(
            {
                "llm": {
                    "provider": "custom",
                    "custom": {
                        "base_url": "https://api.deepseek.com/v1",
                        "model": "deepseek-v4-flash",
                        "wire_api": "chat",
                        "reasoning_effort": "low",
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_helpers, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(config_helpers, "_vault_api_key", lambda _name: "")

    payload = config.get_custom_settings()

    assert payload["configured_reasoning_effort"] == "low"
    assert payload["effective_reasoning_effort"] == ""
    assert payload["reasoning_effort_supported"] is False
    assert payload["context_window"] == 200_000
    assert payload["context_window_source"] == "fallback"
    assert payload["context_window_verified"] is False
