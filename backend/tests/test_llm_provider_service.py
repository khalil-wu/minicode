from __future__ import annotations

import asyncio
import time
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

    async def config_change_hook(**_kwargs) -> None:
        return None

    monkeypatch.setattr(llm_provider_service, "_persist_refreshed_models", lambda *args, **kwargs: None)

    result = asyncio.run(llm_provider_service.refresh_llm_models(
        _request(),
        fetch_anthropic_models=fetch_anthropic,
        fetch_openai_models=fail_openai,
        config_change_hook=config_change_hook,
    ))

    assert calls == ["anthropic:https://api.deepseek.com/anthropic"]
    assert result["provider_id"] == "custom_anthropic"
    assert result["source"] == "live"
    assert result["models"] == ["deepseek-v4-flash", "deepseek-v4-pro"]


def test_custom_anthropic_connection_checks_only_the_messages_transport() -> None:
    calls: list[tuple[str, str]] = []

    async def fetch_anthropic(base_url: str, _api_key: str, **_transport) -> list[str]:
        calls.append(("anthropic-models", base_url))
        return ["deepseek-v4-pro"]

    async def fail_openai(_base_url: str, _api_key: str, **_transport) -> list[str]:
        raise AssertionError("explicit Anthropic wire API must not probe OpenAI discovery")

    async def check_messages(base_url: str, _api_key: str, model: str, **_transport) -> None:
        calls.append(("messages", f"{base_url}::{model}"))

    result = asyncio.run(llm_provider_service.check_llm_connection(
        _request(),
        fetch_anthropic_models=fetch_anthropic,
        fetch_openai_models=fail_openai,
        check_anthropic_generation=check_messages,
    ))

    assert result["ok"] is True
    assert result["provider_id"] == "custom_anthropic"
    assert result["wire_api"] == "anthropic"
    assert calls == [
        ("anthropic-models", "https://api.deepseek.com/anthropic"),
        ("messages", "https://api.deepseek.com/anthropic::deepseek-v4-pro"),
    ]


def test_custom_anthropic_preserves_manual_model_without_cross_protocol_fallback(monkeypatch) -> None:
    calls: list[str] = []

    async def empty_anthropic(base_url: str, _api_key: str, **_transport) -> list[str]:
        calls.append(f"anthropic:{base_url}")
        return []

    async def fail_openai(_base_url: str, _api_key: str, **_transport) -> list[str]:
        raise AssertionError("explicit Anthropic wire API must not probe OpenAI discovery")

    async def config_change_hook(**_kwargs) -> None:
        return None

    monkeypatch.setattr(llm_provider_service, "_persist_refreshed_models", lambda *args, **kwargs: None)

    result = asyncio.run(llm_provider_service.refresh_llm_models(
        _custom_messages_request(),
        fetch_anthropic_models=empty_anthropic,
        fetch_openai_models=fail_openai,
        config_change_hook=config_change_hook,
    ))

    assert calls == ["anthropic:https://gateway.example/v1"]
    assert result["provider_id"] == "custom_anthropic"
    assert result["selected_model"] == "vendor-model-1"
    assert result["models"] == ["vendor-model-1"]


def test_refreshed_models_persist_only_the_target_provider(monkeypatch) -> None:
    saved_payloads: list[dict] = []
    loaded_config = object()
    monkeypatch.setattr(
        llm_provider_helpers,
        "get_llm_settings_payload",
        lambda: {
            "provider": "openai",
            "openai": {
                "base_url": "https://api.openai.com/v1",
                "model": "gpt-5",
            },
            "anthropic": {
                "base_url": "http://127.0.0.1:15721",
                "model": "claude-sonnet-4-6",
            },
            "custom": {
                "display_name": "Work gateway",
                "base_url": "https://gateway.example/v1",
                "model": "vendor-model-1",
                "available_models": ["vendor-model-1"],
                "wire_api": "responses",
            },
        },
    )
    monkeypatch.setattr(
        llm_provider_helpers,
        "save_llm_settings",
        lambda payload: saved_payloads.append(payload),
    )
    monkeypatch.setattr(llm_provider_helpers, "load_config", lambda: loaded_config)

    result = llm_provider_helpers._persist_refreshed_models(
        "custom",
        ["vendor-model-2", "vendor-model-1"],
        "vendor-model-2",
        {
            "vendor-model-2": {
                "context_window": 96_000,
                "reasoning_effort_levels": ["low", "medium"],
                "source": "provider",
            }
        },
    )

    assert result is loaded_config
    assert len(saved_payloads) == 1
    assert set(saved_payloads[0]) == {"custom"}
    assert saved_payloads[0]["custom"] == {
        "display_name": "Work gateway",
        "base_url": "https://gateway.example/v1",
        "model": "vendor-model-2",
        "available_models": ["vendor-model-2", "vendor-model-1"],
        "models_source": "live",
        "model_metadata": {
            "vendor-model-2": {
                "context_window": 96_000,
                "reasoning_effort_levels": ["low", "medium"],
                "source": "provider",
            }
        },
        "wire_api": "responses",
        "reasoning_effort_levels": ["low", "medium"],
    }


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
    persisted: list[tuple] = []

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
    monkeypatch.setattr(
        llm_provider_service,
        "_persist_refreshed_models",
        lambda *args: persisted.append(args),
    )

    async def fetch_models(_base_url: str, _api_key: str, **_transport):
        return llm_provider_helpers.ModelDiscovery(["deepseek-v4-flash"], {})

    async def config_change_hook(**_kwargs):
        return None

    result = asyncio.run(
        llm_provider_service.refresh_llm_models(
            request,
            fetch_openai_models=fetch_models,
            config_change_hook=config_change_hook,
        )
    )

    assert persisted[0][3] == {}
    assert result["model_metadata"] == {}
    assert result["reasoning_effort_levels"] == []
    assert result["configured_reasoning_effort"] == "low"
    assert result["effective_reasoning_effort"] == ""
    assert result["reasoning_effort_supported"] is False
    assert result["context_window"] == 200_000
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
    monkeypatch.setattr(llm_provider_service, "_persist_refreshed_models", lambda *_args: None)

    async def fetch_models(_base_url: str, _api_key: str, **_transport):
        return llm_provider_helpers.ModelDiscovery(["gpt-5.6-sol"], {})

    async def config_change_hook(**_kwargs):
        return None

    result = asyncio.run(
        llm_provider_service.refresh_llm_models(
            request,
            fetch_openai_models=fetch_models,
            config_change_hook=config_change_hook,
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


def test_custom_anthropic_generation_is_authoritative_when_discovery_fails() -> None:
    calls: list[str] = []

    async def fail_anthropic(_base_url: str, _api_key: str, **_transport) -> list[str]:
        calls.append("anthropic-models")
        raise RuntimeError("no Anthropic models endpoint")

    async def fail_openai(_base_url: str, _api_key: str, **_transport) -> list[str]:
        raise AssertionError("explicit Anthropic wire API must not probe OpenAI discovery")

    async def check_messages(base_url: str, _api_key: str, model: str, **_transport) -> None:
        calls.append(f"messages:{base_url}:{model}")

    result = asyncio.run(llm_provider_service.check_llm_connection(
        _custom_messages_request(),
        fetch_anthropic_models=fail_anthropic,
        fetch_openai_models=fail_openai,
        check_anthropic_generation=check_messages,
    ))

    assert result["ok"] is True
    assert result["models"] == ["vendor-model-1"]
    assert calls == [
        "anthropic-models",
        "messages:https://gateway.example/v1:vendor-model-1",
    ]


def test_official_anthropic_empty_base_url_still_posts_to_official_messages(monkeypatch) -> None:
    request: dict[str, object] = {}

    class _Response:
        def raise_for_status(self) -> None:
            return None

    class _Client:
        def __init__(self, **_kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def post(self, url: str, **kwargs):
            request["url"] = url
            request["kwargs"] = kwargs
            return _Response()

    monkeypatch.setattr(llm_provider_helpers.httpx, "AsyncClient", _Client)

    asyncio.run(llm_provider_helpers._check_anthropic_generation(
        "",
        "anthropic-key",
        "claude-sonnet-4-6",
        headers={},
        auth_header=False,
    ))

    assert request["url"] == "https://api.anthropic.com/v1/messages"
    assert request["kwargs"]["json"]["model"] == "claude-sonnet-4-6"
    assert request["kwargs"]["json"]["max_tokens"] == 64


def test_openai_chat_generation_probe_does_not_use_one_token(monkeypatch) -> None:
    request: dict[str, object] = {}

    class _Response:
        def raise_for_status(self) -> None:
            return None

    class _Client:
        def __init__(self, **_kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def post(self, url: str, **kwargs):
            request["url"] = url
            request["kwargs"] = kwargs
            return _Response()

    monkeypatch.setattr(llm_provider_helpers.httpx, "AsyncClient", _Client)

    asyncio.run(
        llm_provider_helpers._check_openai_compatible_generation(
            "https://gateway.example/v1",
            "provider-key",
            "glm-5.2",
            "chat",
            headers={},
            auth_header=False,
        )
    )

    assert request["url"] == "https://gateway.example/v1/chat/completions"
    assert request["kwargs"]["json"]["max_tokens"] == 64


def test_responses_generation_probe_is_bounded_like_its_chat_sibling() -> None:
    """"Test connection" must not spin forever on a half-open endpoint.

    The chat branch bounds itself with ``httpx.AsyncClient(timeout=15.0)``. The
    responses branch drives a full OpenAIAdapter whose client has a 600s read
    timeout, so without an explicit ceiling the settings dialog hangs for ten
    minutes against a server that accepts the TCP connection and never replies.
    """

    async def scenario() -> tuple[float, str]:
        server = await asyncio.start_server(lambda _r, _w: None, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        started = time.monotonic()
        try:
            await asyncio.wait_for(
                llm_provider_helpers._check_openai_compatible_generation(
                    f"http://127.0.0.1:{port}/v1",
                    "provider-key",
                    "gpt-5.4",
                    "responses",
                    headers={},
                    auth_header=False,
                ),
                # Generously past the probe's own ceiling; if this fires the
                # probe is unbounded.
                timeout=llm_provider_helpers._GENERATION_CHECK_TIMEOUT_SECONDS + 20,
            )
        except RuntimeError as exc:
            return time.monotonic() - started, str(exc)
        finally:
            server.close()
        raise AssertionError("probe returned success against a half-open endpoint")

    elapsed, message = asyncio.run(scenario())

    assert "timed out" in message
    assert elapsed < llm_provider_helpers._GENERATION_CHECK_TIMEOUT_SECONDS + 10


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

    async def config_change_hook(**_kwargs) -> None:
        return None

    monkeypatch.setattr(llm_provider_service, "_persist_refreshed_models", lambda *_args: None)

    result = asyncio.run(
        llm_provider_service.refresh_llm_models(
            request,
            fetch_openai_models=fetch_models,
            config_change_hook=config_change_hook,
        )
    )

    assert seen == ["direct"]
    assert result["proxy_mode"] == "direct"


@pytest.mark.parametrize("wire_api", ["chat", "responses"])
def test_direct_proxy_mode_covers_text_discovery_generation_and_images(
    monkeypatch,
    wire_api: str,
) -> None:
    section = SimpleNamespace(
        api_key="test-key",
        base_url="https://gateway.example/v1",
        model="gateway-model",
        wire_api=wire_api,
        proxy_mode="direct",
        available_models=["gateway-model"],
        model_metadata={},
        image_mode="custom",
        image_api_key="test-image-key",
        image_base_url="https://images.example/v1",
        image_model="image-model",
        image_size="1024x1024",
        image_quality="",
    )
    request = SimpleNamespace(
        provider="custom",
        custom=section,
        openai=section,
        anthropic=section,
    )
    seen: list[tuple[str, str]] = []

    async def fetch_models(
        _base_url: str,
        _api_key: str,
        *,
        proxy_mode: str,
        **_transport,
    ) -> list[str]:
        seen.append(("models", proxy_mode))
        return ["gateway-model"]

    async def check_text(
        _base_url: str,
        _api_key: str,
        _model: str,
        selected_wire_api: str,
        *,
        proxy_mode: str,
        **_transport,
    ) -> None:
        seen.append((f"text:{selected_wire_api}", proxy_mode))

    async def check_image(
        _base_url: str,
        _api_key: str,
        _model: str,
        _size: str,
        _quality: str,
        *,
        proxy_mode: str,
        **_transport,
    ) -> None:
        seen.append(("image", proxy_mode))

    result = asyncio.run(
        llm_provider_service.check_llm_connection(
            request,
            fetch_openai_models=fetch_models,
            check_openai_generation=check_text,
            check_image_generation=check_image,
        )
    )

    assert result["ok"] is True
    assert result["proxy_mode"] == "direct"
    assert seen == [
        ("models", "direct"),
        (f"text:{wire_api}", "direct"),
        ("image", "direct"),
    ]


def test_direct_proxy_mode_covers_anthropic_models_and_messages() -> None:
    section = SimpleNamespace(
        api_key="test-key",
        base_url="https://messages.example/v1",
        model="claude-model",
        wire_api="anthropic",
        proxy_mode="direct",
        available_models=["claude-model"],
        model_metadata={},
        image_mode="disabled",
    )
    request = SimpleNamespace(
        provider="anthropic",
        custom=section,
        openai=section,
        anthropic=section,
    )
    seen: list[tuple[str, str]] = []

    async def fetch_models(
        _base_url: str,
        _api_key: str,
        *,
        proxy_mode: str,
        **_transport,
    ) -> list[str]:
        seen.append(("models", proxy_mode))
        return ["claude-model"]

    async def check_messages(
        _base_url: str,
        _api_key: str,
        _model: str,
        *,
        proxy_mode: str,
        **_transport,
    ) -> None:
        seen.append(("messages", proxy_mode))

    result = asyncio.run(
        llm_provider_service.check_llm_connection(
            request,
            fetch_anthropic_models=fetch_models,
            check_anthropic_generation=check_messages,
        )
    )

    assert result["ok"] is True
    assert result["proxy_mode"] == "direct"
    assert seen == [("models", "direct"), ("messages", "direct")]


def test_omitted_proxy_mode_preserves_saved_direct_profile(monkeypatch) -> None:
    monkeypatch.setattr(
        llm_provider_service,
        "get_custom_settings",
        lambda: {
            "api_key": "stored-key",
            "base_url": "https://gateway.example/v1",
            "model": "gateway-model",
            "wire_api": "chat",
            "proxy_mode": "direct",
            "image_mode": "disabled",
            "image_model": "",
        },
    )
    request = LLMSettingsUpdateRequest(
        provider="custom",
        custom={
            "api_key": "test-key",
            "base_url": "https://gateway.example/v1",
            "model": "gateway-model",
            "wire_api": "chat",
            "image_mode": "disabled",
        },
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

    async def check_text(
        _base_url: str,
        _api_key: str,
        _model: str,
        _wire_api: str,
        *,
        proxy_mode: str,
        **_transport,
    ) -> None:
        seen.append(proxy_mode)

    result = asyncio.run(
        llm_provider_service.check_llm_connection(
            request,
            fetch_openai_models=fetch_models,
            check_openai_generation=check_text,
        )
    )

    assert result["proxy_mode"] == "direct"
    assert seen == ["direct", "direct"]
