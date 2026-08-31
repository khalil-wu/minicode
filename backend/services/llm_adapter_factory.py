from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from typing import TYPE_CHECKING

from backend.config import (
    MINICODE_CAPPED_DEFAULT_MAX_TOKENS,
    LLMSettings,
    get_anthropic_settings,
    get_custom_settings,
    get_llm_provider,
    get_openai_settings,
    get_provider_model_metadata,
)
from backend.llm.base import LLMAdapter
from backend.llm.openai_adapter import OpenAIAdapter

if TYPE_CHECKING:
    from backend.config import AppConfig
    from backend.llm.model_runtime import ModelRuntime
    from backend.llm.provider_contracts import ProviderAdapterSpec

logger = logging.getLogger(__name__)


def build_wire_adapter(
    settings: LLMSettings,
    *,
    thinking_budget: int | None = None,
    cache_editing_beta_header: str = "",
    provider_id: str | None = None,
) -> LLMAdapter:
    """Construct one mature adapter from an explicit provider wire contract.

    This is the shared production/evaluation boundary: OpenAI Chat and
    Responses stay on ``OpenAIAdapter``; Anthropic Messages stays on
    ``AnthropicAdapter``. Unknown contracts fail closed instead of silently
    falling back to a different protocol.
    """

    wire_api = str(settings.wire_api or "").strip().lower()
    if wire_api in {"chat", "responses"}:
        return OpenAIAdapter(settings=settings)
    if wire_api == "anthropic":
        from backend.llm.anthropic_adapter import AnthropicAdapter

        resolved_provider_id = str(
            provider_id or settings.provider or "custom"
        ).strip() or "custom"
        return AnthropicAdapter(
            api_key=settings.api_key,
            model=settings.model,
            small_fast_model=settings.small_fast_model,
            base_url=settings.base_url or None,
            max_tokens=max(
                1,
                settings.max_tokens
                or MINICODE_CAPPED_DEFAULT_MAX_TOKENS,
            ),
            context_window=settings.context_window,
            thinking_budget=thinking_budget,
            use_auth_token=bool(settings.auth_header),
            cache_editing_beta_header=cache_editing_beta_header,
            default_headers=dict(settings.default_headers),
            provider_id=resolved_provider_id,
            proxy_mode=str(getattr(settings, "proxy_mode", "inherit") or "inherit"),
        )
    raise ValueError(f"Unsupported LLM wire API: {wire_api or '<empty>'}")


def _section_default_headers(section: Mapping[str, object]) -> tuple[tuple[str, str], ...]:
    raw_headers = section.get("default_headers") or section.get("headers") or {}
    if isinstance(raw_headers, Mapping):
        return tuple(
            (str(key), str(value))
            for key, value in raw_headers.items()
            if str(key).strip()
        )
    if isinstance(raw_headers, (list, tuple)):
        return tuple(
            (str(item[0]), str(item[1]))
            for item in raw_headers
            if isinstance(item, (list, tuple))
            and len(item) == 2
            and str(item[0]).strip()
        )
    return ()


def _openai_compatible_settings(
    section: dict[str, object],
    *,
    provider: str,
    model_override: str | None,
) -> LLMSettings:
    selected_model = str(model_override or section.get("model") or "").strip()
    from backend.llm.capabilities import is_gpt_image_model

    dedicated_image_model = is_gpt_image_model(selected_model)
    image_size = str(section.get("image_size") or "1024x1024")
    image_quality = str(section.get("image_quality") or "")
    image_model = ""
    api_key = str(section.get("api_key") or "")
    base_url = str(section.get("base_url") or "")
    wire_api = str(section.get("wire_api") or "chat")
    if dedicated_image_model:
        image_mode = str(section.get("image_mode") or "inherit").strip().lower()
        if image_mode == "disabled":
            raise ValueError("Image generation is disabled for this provider profile")
        if image_mode == "custom":
            api_key = str(section.get("image_api_key") or "")
            base_url = str(section.get("image_base_url") or "")
        elif wire_api.strip().lower() == "anthropic":
            raise ValueError(
                "Anthropic Messages cannot be inherited as an Images API; "
                "configure an independent OpenAI-compatible image channel"
            )
        image_model = str(section.get("image_model") or selected_model).strip()
        if not base_url.strip():
            raise ValueError("Dedicated image model requires an Images API base URL")
        if not image_model:
            raise ValueError("Dedicated image model requires an Images API model id")
        # A custom profile can retain an Anthropic Messages transport for its
        # text models, but a dedicated image model always uses the
        # OpenAI-compatible Images API boundary.
        wire_api = "chat"
    metadata = get_provider_model_metadata(section, selected_model)
    return LLMSettings(
        api_key=api_key,
        provider=provider,
        base_url=base_url,
        model=selected_model,
        small_fast_model=str(section.get("small_fast_model") or ""),
        reasoning_effort=str(section.get("reasoning_effort") or ""),
        responses_reasoning_summary=str(
            section.get("responses_reasoning_summary") or "off"
        ),
        max_tokens=max(0, int(section.get("max_tokens") or 0)),
        wire_api=wire_api,
        proxy_mode=str(section.get("proxy_mode") or "inherit"),
        prompt_cache_retention=str(section.get("prompt_cache_retention") or ""),
        reasoning_effort_levels=tuple(metadata["reasoning_effort_levels"]),
        context_window=int(metadata["context_window"]),
        context_window_source=str(metadata["context_window_source"]),
        context_window_verified=bool(metadata["context_window_verified"]),
        max_context_window=int(metadata["max_context_window"]),
        max_context_window_source=str(metadata["max_context_window_source"]),
        max_context_window_verified=bool(metadata["max_context_window_verified"]),
        max_output_tokens=int(metadata["max_output_tokens"]),
        max_output_tokens_source=str(metadata["max_output_tokens_source"]),
        max_output_tokens_verified=bool(metadata["max_output_tokens_verified"]),
        default_reasoning_effort=str(metadata["default_reasoning_effort"]),
        default_reasoning_summary=str(metadata["default_reasoning_summary"]),
        image_model=image_model,
        image_size=image_size,
        image_quality=image_quality,
        default_headers=_section_default_headers(section),
        auth_header=bool(section.get("auth_header", section.get("authHeader", False))),
    )


def build_provider_adapter(
    provider: str,
    model_override: str | None = None,
    *,
    model_runtime: "ModelRuntime | None" = None,
) -> LLMAdapter:
    """Construct one provider adapter, or fail explicitly."""
    requested_provider = (provider or "").strip() or get_llm_provider()
    # Provider ids are registry keys and remain case-sensitive. Built-in MiniCode
    # settings retain their normalized lowercase ids, while an extension-owned
    # ModelRuntime must receive the exact id registered by the extension.
    normalized = (
        requested_provider
        if model_runtime is not None
        else requested_provider.lower()
    )

    if model_runtime is not None:
        models = model_runtime.get_models(normalized)
        model_id = str(model_override or "").strip()
        if not model_id:
            raise ValueError(
                f"Selected provider '{normalized}' requires an explicit model selection"
            )
        if not models:
            raise ValueError(f"Selected provider '{normalized}' has no model configuration")
        spec = model_runtime.resolve_adapter_spec(normalized, model_id)
        return _build_registered_provider_adapter(spec)

    if normalized == "anthropic":
        anthropic_settings = get_anthropic_settings()
        api_key = anthropic_settings["api_key"]

        model = (model_override or anthropic_settings["model"]).strip()
        if not model:
            raise ValueError("Selected provider 'anthropic' requires an explicit model selection")
        base_url = anthropic_settings["base_url"] or None
        max_tokens = max(
            1,
            int(anthropic_settings["max_tokens"] or MINICODE_CAPPED_DEFAULT_MAX_TOKENS),
        )
        # Transport follows the explicit provider selection. Do not infer
        # it from a hostname; custom Messages gateways use the `custom`
        # branch below.
        return build_wire_adapter(
            LLMSettings(
                api_key=api_key,
                provider="anthropic",
                base_url=str(base_url or ""),
                model=model,
                small_fast_model=str(
                    anthropic_settings["small_fast_model"] or ""
                ),
                max_tokens=max_tokens,
                wire_api="anthropic",
                proxy_mode=str(anthropic_settings.get("proxy_mode") or "inherit"),
                context_window=int(anthropic_settings.get("context_window") or 0),
                default_headers=tuple(anthropic_settings.get("default_headers") or ()),
                auth_header=bool(anthropic_settings.get("auth_header")),
            ),
            thinking_budget=int(anthropic_settings["thinking_budget"]) or None,
            cache_editing_beta_header=os.getenv(
                "MINICODE_ANTHROPIC_CACHE_EDITING_BETA_HEADER", ""
            ),
            provider_id="anthropic",
        )

    if normalized in ("openai", "custom"):
        if normalized == "custom":
            custom = get_custom_settings()
            from backend.llm.capabilities import is_gpt_image_model

            selected_model = str(model_override or custom.get("model") or "").strip()
            if not selected_model:
                raise ValueError("Selected provider 'custom' requires an explicit model selection")
            if custom["wire_api"] == "anthropic" and not is_gpt_image_model(selected_model):
                return build_wire_adapter(
                    _openai_compatible_settings(
                        custom,
                        provider="custom",
                        model_override=model_override,
                    ),
                    thinking_budget=int(custom["thinking_budget"]) or None,
                    cache_editing_beta_header=os.getenv(
                        "MINICODE_ANTHROPIC_CACHE_EDITING_BETA_HEADER", ""
                    ),
                    provider_id="custom",
                )
            openai_settings = _openai_compatible_settings(
                custom,
                provider="custom",
                model_override=model_override,
            )
        else:
            if not str(model_override or get_openai_settings().get("model") or "").strip():
                raise ValueError("Selected provider 'openai' requires an explicit model selection")
            openai_settings = _openai_compatible_settings(
                get_openai_settings(),
                provider="openai",
                model_override=model_override,
            )
        return build_wire_adapter(openai_settings)

    raise ValueError(f"Unknown LLM provider '{requested_provider}'")


def _build_registered_provider_adapter(spec: "ProviderAdapterSpec") -> LLMAdapter:
    """Map a registered provider contract onto MiniCode transport adapters."""

    if spec.api == "anthropic-messages":
        from backend.llm.anthropic_adapter import AnthropicAdapter

        return AnthropicAdapter(
            api_key=spec.api_key,
            model=spec.model_id,
            small_fast_model=spec.small_fast_model or spec.model_id,
            base_url=spec.base_url or None,
            max_tokens=max(
                1,
                spec.max_tokens or MINICODE_CAPPED_DEFAULT_MAX_TOKENS,
            ),
            context_window=spec.context_window,
            thinking_budget=spec.thinking_budget,
            cache_editing_beta_header="",
            default_headers=spec.headers,
            provider_id=spec.provider_id,
            proxy_mode=spec.proxy_mode,
        )

    if spec.api in {"openai-responses", "openai-completions"}:
        settings = LLMSettings(
            api_key=spec.api_key,
            provider=spec.provider_id,
            base_url=spec.base_url,
            model=spec.model_id,
            small_fast_model=spec.small_fast_model or spec.model_id,
            reasoning_effort=spec.reasoning_effort,
            responses_reasoning_summary=spec.responses_reasoning_summary,
            # Zero is MiniCode's persisted Auto/Unset sentinel for OpenAI
            # transports. Preserve it through the factory so Responses can
            # omit max_output_tokens and Chat can omit both output-cap fields
            # instead of accidentally issuing a one-token request.
            max_tokens=spec.max_tokens,
            wire_api=(
                "responses" if spec.api == "openai-responses" else "chat"
            ),
            proxy_mode=spec.proxy_mode,
            prompt_cache_retention=spec.prompt_cache_retention,
            reasoning_effort_levels=tuple(spec.reasoning_effort_levels),
            context_window=spec.context_window,
            context_window_source=spec.context_window_source,
            context_window_verified=spec.context_window_verified,
            max_context_window=spec.max_context_window,
            max_context_window_source=spec.max_context_window_source,
            max_context_window_verified=spec.max_context_window_verified,
            max_output_tokens=spec.max_output_tokens,
            max_output_tokens_source=spec.max_output_tokens_source,
            max_output_tokens_verified=spec.max_output_tokens_verified,
            default_reasoning_effort=spec.default_reasoning_effort,
            default_reasoning_summary=spec.default_reasoning_summary,
            default_headers=tuple(
                (str(key), str(value)) for key, value in spec.headers.items()
            ),
            auth_header=bool(spec.auth_header),
        )
        return OpenAIAdapter(settings=settings)

    # ModelRuntime validates this before publication. Keep the construction
    # boundary fail-closed if a future runtime accidentally passes a new API.
    raise RuntimeError(f"Unsupported registered provider API: {spec.api}")


def create_session_llm(
    config: "AppConfig",
    model_override: str | None = None,
    *,
    provider_override: str | None = None,
    model_runtime: "ModelRuntime | None" = None,
) -> LLMAdapter:
    """Create the one explicitly selected provider transport for this session."""
    # ``config`` is unused: every provider section is read back from the
    # persisted settings payload inside ``build_provider_adapter``. The
    # positional slot is part of the composition-root factory contract
    # (backend/bootstrap/app.py always calls ``factory(effective_config, ...)``)
    # so it stays until that DI signature is renegotiated.
    del config
    requested_provider = str(provider_override or get_llm_provider()).strip()
    primary_provider = (
        requested_provider
        if model_runtime is not None
        else requested_provider.lower()
    )
    primary = build_provider_adapter(
        primary_provider,
        model_override=model_override,
        model_runtime=model_runtime,
    )

    return primary
