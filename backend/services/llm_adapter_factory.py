from __future__ import annotations

import logging
import os
from dataclasses import replace
from typing import TYPE_CHECKING

from backend.config import (
    CLAUDE_CODE_CAPPED_DEFAULT_MAX_TOKENS,
    LLMSettings,
    get_anthropic_settings,
    get_custom_settings,
    get_llm_provider,
    load_llm_settings,
)
from backend.llm.base import LLMAdapter
from backend.llm.openai_adapter import OpenAIAdapter

if TYPE_CHECKING:
    from backend.config import AppConfig

logger = logging.getLogger(__name__)


def build_provider_adapter(
    provider: str,
    model_override: str | None = None,
) -> LLMAdapter | None:
    """Construct one provider adapter, returning None when it is unavailable."""
    normalized = (provider or "").strip().lower()

    if normalized == "anthropic":
        try:
            from backend.llm.anthropic_adapter import AnthropicAdapter

            anthropic_settings = get_anthropic_settings()
            api_key = anthropic_settings["api_key"]
            if not api_key:
                return None

            model = (model_override or anthropic_settings["model"]).strip()
            base_url = anthropic_settings["base_url"] or None
            max_tokens = max(
                1,
                int(anthropic_settings["max_tokens"] or CLAUDE_CODE_CAPPED_DEFAULT_MAX_TOKENS),
            )
            # Transport follows the explicit provider selection. Do not infer
            # it from a hostname; custom Messages gateways use the `custom`
            # branch below.
            use_raw_http = False

            return AnthropicAdapter(
                api_key=api_key,
                model=model,
                base_url=base_url,
                max_tokens=max_tokens,
                thinking_budget=int(anthropic_settings["thinking_budget"]) or None,
                use_raw_http=use_raw_http,
                cache_editing_beta_header=os.getenv(
                    "MINICODE_ANTHROPIC_CACHE_EDITING_BETA_HEADER", ""
                ),
            )
        except Exception as exc:
            logger.warning("Anthropic adapter construction failed: %s", exc)
            return None

    if normalized in ("openai", "custom", ""):
        try:
            if normalized == "custom":
                custom = get_custom_settings()
                if not custom["api_key"]:
                    return None
                if custom["wire_api"] == "anthropic":
                    from backend.llm.anthropic_adapter import AnthropicAdapter

                    return AnthropicAdapter(
                        api_key=custom["api_key"],
                        model=(model_override or custom["model"]).strip(),
                        base_url=custom["base_url"] or None,
                        max_tokens=max(
                            1,
                            int(custom["max_tokens"] or CLAUDE_CODE_CAPPED_DEFAULT_MAX_TOKENS),
                        ),
                        thinking_budget=int(custom["thinking_budget"]) or None,
                        use_raw_http=True,
                        cache_editing_beta_header=os.getenv(
                            "MINICODE_ANTHROPIC_CACHE_EDITING_BETA_HEADER", ""
                        ),
                    )
                openai_settings = LLMSettings(
                    api_key=custom["api_key"],
                    provider="custom",
                    base_url=custom["base_url"],
                    model=custom["model"],
                    reasoning_effort=custom["reasoning_effort"],
                    responses_reasoning_summary=custom["responses_reasoning_summary"],
                    max_tokens=custom["max_tokens"],
                    wire_api=custom["wire_api"],
                    responses_stateful_continuation=custom["responses_stateful_continuation"],
                    prompt_cache_retention=custom["prompt_cache_retention"],
                    reasoning_effort_levels=tuple(custom.get("reasoning_effort_levels") or ()),
                )
            else:
                openai_settings = load_llm_settings()
        except Exception as exc:
            logger.warning("OpenAI-compatible adapter construction failed: %s", exc)
            return None
        if model_override:
            openai_settings = replace(openai_settings, model=model_override)
        if not openai_settings.api_key:
            return None
        return OpenAIAdapter(settings=openai_settings)

    logger.warning("Unknown LLM provider '%s'; ignoring it", provider)
    return None


def create_llm_adapter(
    config: "AppConfig",
    model_override: str | None = None,
) -> LLMAdapter:
    """Create the configured LLM adapter, wrapping fallback providers if any."""
    primary_provider = get_llm_provider()
    primary = build_provider_adapter(primary_provider, model_override=model_override)
    if primary is None:
        if primary_provider == "anthropic":
            raise RuntimeError("使用 Anthropic 需要设置 ANTHROPIC_API_KEY")
        if primary_provider == "custom":
            raise RuntimeError("Missing API key for custom provider")
        raise RuntimeError("Missing OPENAI_API_KEY")

    fallbacks: list[LLMAdapter] = []
    seen_providers = {primary_provider}
    for provider_name in (config.agent.fallback_providers or ()):
        if provider_name in seen_providers:
            continue
        seen_providers.add(provider_name)
        adapter = build_provider_adapter(provider_name)
        if adapter is not None:
            fallbacks.append(adapter)

    if not fallbacks:
        return primary

    from backend.llm.fallback_adapter import FallbackLLMAdapter

    return FallbackLLMAdapter([primary, *fallbacks])


def create_session_llm(
    config: "AppConfig",
    model_override: str | None = None,
) -> LLMAdapter:
    """Create the LLM owned by one websocket session."""
    return create_llm_adapter(config, model_override=model_override)
