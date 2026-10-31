from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from backend.bootstrap.app import AppBootstrap
from backend.api.models import LLMSettingsUpdateRequest
from backend.config import AppConfig, LLMSettings
from backend.services import llm_provider_service
from backend.services import llm_provider_helpers


def _request() -> SimpleNamespace:
    section = SimpleNamespace(
        api_key="test-key",
        base_url="https://api.deepseek.com/anthropic",
        model="deepseek-v4-pro",
        wire_api="anthropic",
        available_models=["deepseek-v4-pro"],
        model_metadata={},
    )
    return SimpleNamespace(provider="custom", custom=section, openai=section, anthropic=section)


def _custom_messages_request(*, base_url: str = "https://gateway.example/v1") -> SimpleNamespace:
    section = SimpleNamespace(
        api_key="test-key",
        base_url=base_url,
        model="vendor-model-1",
        wire_api="anthropic",
        available_models=["vendor-model-1"],
        model_metadata={},
    )
    return SimpleNamespace(provider="custom", custom=section, openai=section, anthropic=section)


def test_custom_anthropic_discovers_models_only_through_anthropic_endpoint(monkeypatch) -> None:
    calls: list[str] = []

    async def fetch_anthropic(base_url: str, _api_key: str, **_transport) -> list[str]:
        calls.append(f"anthropic:{base_url}")
        return ["deepseek-v4-flash", "deepseek-v4-pro"]

    async def fail_openai(_base_url: str, _api_key: str, **_transport) -> list[str]:
        raise AssertionError("explicit Anthropic wire API must not probe OpenAI discovery")

    result = asyncio.run(llm_provider_service.refresh_llm_models(
        _request(),
        fetch_anthropic_models=fetch_anthropic,
        fetch_openai_models=fail_openai,
    ))

    assert calls == ["anthropic:https://api.deepseek.com/anthropic"]
    assert result["provider_id"] == "custom_anthropic"
    assert result["source"] == "live"
    assert result["models"] == ["deepseek-v4-flash", "deepseek-v4-pro"]




def test_custom_anthropic_preserves_manual_model_without_cross_protocol_fallback(monkeypatch) -> None:
    calls: list[str] = []

    async def empty_anthropic(base_url: str, _api_key: str, **_transport) -> list[str]:
        calls.append(f"anthropic:{base_url}")
        return []

    async def fail_openai(_base_url: str, _api_key: str, **_transport) -> list[str]:
        raise AssertionError("explicit Anthropic wire API must not probe OpenAI discovery")

    result = asyncio.run(llm_provider_service.refresh_llm_models(
        _custom_messages_request(),
        fetch_anthropic_models=empty_anthropic,
        fetch_openai_models=fail_openai,
    ))

    assert calls == ["anthropic:https://gateway.example/v1"]
    assert result["provider_id"] == "custom_anthropic"
    assert result["selected_model"] == "vendor-model-1"
    assert result["models"] == ["vendor-model-1"]


@pytest.mark.parametrize("provider,wire_api", [
    ("custom", "chat"), ("custom", "responses"), ("custom", "anthropic"),
    ("openai", "responses"), ("anthropic", "anthropic"),
])
def test_model_discovery_keeps_draft_candidates_out_of_saved_profiles(tmp_path, provider, wire_api) -> None:
    from backend import config_helpers
    from backend.config import get_llm_settings_payload, save_llm_settings

    save_llm_settings({
        "provider": provider,
        provider: {
            "base_url": "https://saved.example/v1",
            "api_key": "saved-test-key",
            "model": "saved-model",
            "available_models": ["saved-model"],
            "wire_api": wire_api,
            "reasoning_effort": "high",
            "model_metadata": {"saved-model": {"context_window": 96_000}},
        },
    })
    before_payload = get_llm_settings_payload()
    settings_file = config_helpers.SETTINGS_FILE
    before_file = settings_file.read_bytes()
    calls = []

    async def discover(base_url, api_key, **_transport):
        calls.append((base_url, api_key))
        return llm_provider_helpers.ModelDiscovery(
            ["draft-model"], {"draft-model": {"context_window": 128_000}},
        )

    result = asyncio.run(llm_provider_service.refresh_llm_models(
        LLMSettingsUpdateRequest.model_validate({
            "provider": provider,
            provider: {
                "base_url": "https://draft.example/v1",
                "api_key": "draft-test-key",
                "model": "",
                "wire_api": wire_api,
                "reasoning_effort": "",
                "model_metadata": {},
            },
        }),
        fetch_openai_models=discover,
        fetch_anthropic_models=discover,
    ))

    assert calls == [("https://draft.example/v1", "draft-test-key")]
    assert result["source"] == "live"
    assert result["models"] == ["draft-model"]
    assert result["model_metadata"] == {"draft-model": {"context_window": 128_000}}
    assert result["configured_reasoning_effort"] == ""
    assert "_config" not in result
    assert settings_file.read_bytes() == before_file
    assert get_llm_settings_payload() == before_payload




def test_model_discovery_extracts_reference_client_metadata_shapes() -> None:
    discovery = llm_provider_helpers._extract_model_discovery(
        {
            "data": [
                {
                    "id": "gpt-test",
                    "context_window": 128_000,
                    "supported_reasoning_efforts": [
                        {"reasoning_effort": "low"},
                        {"reasoning_effort": "high"},
                    ],
                },
                {
                    "id": "gateway-model",
                    "capabilities": {
                        "contextLength": 64_000,
                        "reasoning_effort_levels": ["minimal", "medium"],
                    },
                },
                {
                    "id": "claude-capability-shape",
                    "max_input_tokens": 200_000,
                },
                {
                    "id": "codex-core-shape",
                    "supported_reasoning_levels": [
                        {"effort": "low", "description": "Low"},
                        {"effort": "max", "description": "Maximum"},
                        {"effort": "ultra", "description": "Ultra"},
                        {"effort": "focused", "description": "Focused"},
                    ],
                },
                {
                    "id": "codex-app-server-shape",
                    "supportedReasoningEfforts": [
                        {"reasoningEffort": "minimal", "description": "Minimal"},
                        {"reasoningEffort": "xhigh", "description": "Extra high"},
                    ],
                },
            ]
        }
    )

    assert list(discovery) == [
        "gpt-test",
        "gateway-model",
        "claude-capability-shape",
        "codex-core-shape",
        "codex-app-server-shape",
    ]
    assert discovery.model_metadata == {
        "gpt-test": {
            "context_window": 128_000,
            "reasoning_effort_levels": ["low", "high"],
            "source": "provider",
        },
        "gateway-model": {
            "context_window": 64_000,
            "reasoning_effort_levels": ["minimal", "medium"],
            "source": "provider",
        },
        "claude-capability-shape": {
            "context_window": 200_000,
            "source": "provider",
        },
        "codex-core-shape": {
            "reasoning_effort_levels": ["low", "max", "ultra", "focused"],
            "source": "provider",
        },
        "codex-app-server-shape": {
            "reasoning_effort_levels": ["minimal", "xhigh"],
            "source": "provider",
        },
    }


def test_live_refresh_clears_stale_capabilities_when_provider_declares_none(
    monkeypatch,
) -> None:
    section = SimpleNamespace(
        api_key="test-key",
        base_url="https://gateway.example/v1",
        model="deepseek-v4-flash",
        wire_api="chat",
        available_models=["deepseek-v4-flash"],
        model_metadata={
            "deepseek-v4-flash": {
                "context_window": 128_000,
                "reasoning_effort_levels": ["low"],
            }
        },
    )
    request = SimpleNamespace(
        provider="custom",
        custom=section,
        openai=section,
        anthropic=section,
    )

    monkeypatch.setattr(
        llm_provider_service,
        "get_custom_settings",
        lambda: {
            "api_key": "test-key",
            "base_url": "https://gateway.example/v1",
            "model": "deepseek-v4-flash",
            "wire_api": "chat",
            "reasoning_effort": "low",
            "reasoning_effort_levels": ["low"],
            "model_metadata": section.model_metadata,
        },
    )
    async def fetch_models(_base_url: str, _api_key: str, **_transport):
        return llm_provider_helpers.ModelDiscovery(["deepseek-v4-flash"], {})

    result = asyncio.run(
        llm_provider_service.refresh_llm_models(
            request,
            fetch_openai_models=fetch_models,
        )
    )

    assert result["model_metadata"] == {}
    assert result["reasoning_effort_levels"] == []
    assert result["configured_reasoning_effort"] == "low"
    assert result["effective_reasoning_effort"] == ""
    assert result["reasoning_effort_supported"] is False
    assert result["context_window"] == 272_000
    assert result["context_window_source"] == "fallback"
    assert result["context_window_verified"] is False


def test_live_refresh_uses_exact_known_responses_efforts_when_catalog_omits_metadata(
    monkeypatch,
) -> None:
    section = SimpleNamespace(
        api_key="test-key",
        base_url="https://gateway.example/v1",
        model="gpt-5.6-sol",
        wire_api="responses",
        available_models=["gpt-5.6-sol"],
        model_metadata={},
    )
    request = SimpleNamespace(
        provider="custom",
        custom=section,
        openai=section,
        anthropic=section,
    )

    monkeypatch.setattr(
        llm_provider_service,
        "get_custom_settings",
        lambda: {
            "api_key": "test-key",
            "base_url": "https://gateway.example/v1",
            "model": "gpt-5.6-sol",
            "wire_api": "responses",
            "reasoning_effort": "xhigh",
            "reasoning_effort_levels": [],
            "model_metadata": {},
        },
    )
    async def fetch_models(_base_url: str, _api_key: str, **_transport):
        return llm_provider_helpers.ModelDiscovery(["gpt-5.6-sol"], {})

    result = asyncio.run(
        llm_provider_service.refresh_llm_models(
            request,
            fetch_openai_models=fetch_models,
        )
    )

    assert result["reasoning_effort_levels"] == [
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
        "ultra",
    ]
    assert result["configured_reasoning_effort"] == "xhigh"
    assert result["effective_reasoning_effort"] == "xhigh"
    assert result["reasoning_effort_supported"] is True


def test_reasoning_efforts_preserve_explicit_chat_contract_without_catalog_leaks() -> None:
    from backend.llm.reasoning_effort import reasoning_effort_levels

    assert reasoning_effort_levels("gpt-5.6-sol", "chat", []) == ()
    assert reasoning_effort_levels(
        "gpt-5.6-sol",
        "chat",
        ["low", "medium", "high", "xhigh"],
    ) == ("low", "medium", "high", "xhigh")
    assert reasoning_effort_levels("my-gpt-5.6-sol", "responses", []) == ()
    assert reasoning_effort_levels("openai/gpt-5.6-sol", "responses", []) == (
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
        "ultra",
    )
    assert reasoning_effort_levels(
        "gpt-5.6-sol",
        "responses",
        ["low", "focused"],
    ) == ("low", "focused")


def test_custom_chat_runtime_projects_declared_reasoning_efforts() -> None:
    from backend.llm.capabilities import capabilities_from_openai_settings

    capabilities = capabilities_from_openai_settings(
        LLMSettings(
            api_key="test-key",
            model="gpt-5.6-terra",
            wire_api="chat",
            reasoning_effort="medium",
            reasoning_effort_levels=("low", "medium", "high", "xhigh"),
        ),
        provider="custom",
    )

    assert capabilities.reasoning_effort_supported is True
    assert capabilities.reasoning_effort_levels == (
        "low",
        "medium",
        "high",
        "xhigh",
    )
    assert capabilities.effective_reasoning_effort == "medium"










def test_session_llm_factory_type_error_is_not_retried() -> None:
    calls: list[str | None] = []

    def create_provider(_config, *, model_override=None, provider_override=None):
        assert provider_override == "custom"
        calls.append(model_override)
        raise TypeError("provider construction failed internally")

    bootstrap = AppBootstrap(
        build_tool_registry=lambda *_args, **_kwargs: None,
        create_session_llm=create_provider,
        ws_manager=SimpleNamespace(),
        on_mcp_status_change=lambda *_args, **_kwargs: None,
    )
    bootstrap.config = AppConfig(llm=LLMSettings(api_key=""))

    with pytest.raises(TypeError, match="failed internally"):
        bootstrap.create_llm(model_override="vendor-model-1")

    assert calls == ["vendor-model-1"]


def test_direct_proxy_mode_is_used_for_refresh_discovery(monkeypatch) -> None:
    section = SimpleNamespace(
        api_key="test-key",
        base_url="https://gateway.example/v1",
        model="gateway-model",
        wire_api="chat",
        proxy_mode="direct",
        available_models=["gateway-model"],
        model_metadata={},
    )
    request = SimpleNamespace(
        provider="custom",
        custom=section,
        openai=section,
        anthropic=section,
    )
    seen: list[str] = []

    async def fetch_models(
        _base_url: str,
        _api_key: str,
        *,
        proxy_mode: str,
        **_transport,
    ) -> list[str]:
        seen.append(proxy_mode)
        return ["gateway-model"]

    result = asyncio.run(
        llm_provider_service.refresh_llm_models(
            request,
            fetch_openai_models=fetch_models,
        )
    )

    assert seen == ["direct"]
    assert result["proxy_mode"] == "direct"


@pytest.mark.parametrize("wire", ["chat", "anthropic"])
def test_discovery_uses_saved_direct_mode_when_request_omits_it(monkeypatch, wire):
    current = {"api_key": "stored-key", "base_url": "https://gateway.example/v1",
        "model": "gateway-model", "wire_api": wire, "proxy_mode": "direct", "image_mode": "disabled"}
    monkeypatch.setattr(llm_provider_service, "get_custom_settings", lambda: current)
    request = LLMSettingsUpdateRequest.model_validate({"provider": "custom", "custom": {
        "api_key": "fixture", "base_url": current["base_url"], "model": current["model"], "wire_api": wire}})
    seen = []
    async def discover(_base_url, _key, *, proxy_mode, **_transport):
        seen.append(proxy_mode)
        return ["gateway-model"]
    result = asyncio.run(llm_provider_service.refresh_llm_models(request,
        fetch_openai_models=discover, fetch_anthropic_models=discover))
    assert result["proxy_mode"] == "direct" and seen == ["direct"]








@pytest.mark.parametrize("change_target, clear_behavior", [(False, False), (True, False), (False, True)])
def test_live_model_refresh_preserves_only_target_scoped_explicit_behavior(monkeypatch, change_target, clear_behavior):
    current = {
        "base_url": "https://saved.example/v1", "model": "selected", "wire_api": "responses",
        "api_key": "fixture", "model_metadata": {"selected": {
            "supports_custom_tools": True, "native_compaction": False, "model_instructions": "Configured guidance.",
            "context_window": 100_000,
        }},
    }
    monkeypatch.setattr(llm_provider_service, "get_openai_settings", lambda: current)
    incoming = {"api_key": "fixture", "wire_api": "responses",
                "base_url": "https://different.example/v1" if change_target else current["base_url"]}
    if clear_behavior:
        incoming["model_metadata"] = {}
    request = LLMSettingsUpdateRequest.model_validate({"provider": "openai", "openai": incoming})
    async def discover(*args, **kwargs):
        return llm_provider_helpers.ModelDiscovery(["selected"], {"selected": {"context_window": 200_000}})
    result = asyncio.run(llm_provider_service.refresh_llm_models(request, fetch_openai_models=discover))
    metadata = result["model_metadata"]["selected"]
    assert metadata["context_window"] == 200_000
    if not change_target and not clear_behavior:
        assert metadata["supports_custom_tools"] is True
        assert metadata["native_compaction"] is False
        assert metadata["model_instructions"] == "Configured guidance."
    else:
        assert "supports_custom_tools" not in metadata
        assert "native_compaction" not in metadata
        assert "model_instructions" not in metadata


def test_discovery_reads_explicit_native_tool_capability_without_importing_prompt_text():
    result = llm_provider_helpers._extract_model_discovery({"data": [{
        "id": "custom-model", "supports_custom_tools": True, "native_compaction": True,
        "model_instructions": "Remote instructions are not local prompt configuration.",
    }]})
    assert result.model_metadata["custom-model"]["supports_custom_tools"] is True
    assert result.model_metadata["custom-model"]["native_compaction"] is True
    assert "model_instructions" not in result.model_metadata["custom-model"]
