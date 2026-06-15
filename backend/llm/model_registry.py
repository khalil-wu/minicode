"""
LLM provider adapter construction and session-level LLM creation.

Extracted from main.py to keep model/provider logic in a single,
cohesive module inside the llm package.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from backend.config import (
    get_anthropic_settings,
    get_custom_settings,
    get_llm_provider,
    load_llm_settings,
    LLMSettings,
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
    """按 provider 名称构造单个 LLM 适配器，构造失败时返回 None。"""
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
            max_tokens = int(anthropic_settings["max_tokens"])
            use_raw_http = bool(base_url and "api.anthropic.com" not in base_url.lower())

            return AnthropicAdapter(
                api_key=api_key,
                model=model,
                base_url=base_url,
                max_tokens=max_tokens,
                thinking_budget=int(anthropic_settings["thinking_budget"]) or None,
                use_raw_http=use_raw_http,
            )
        except Exception as exc:
            logger.warning("Anthropic adapter 构造失败: %s", exc)
            return None

    if normalized in ("openai", "custom", ""):
        try:
            if normalized == "custom":
                custom = get_custom_settings()
                if not custom["api_key"]:
                    return None
                if custom["wire_api"] == "anthropic":
                    return AnthropicAdapter(
                        api_key=custom["api_key"],
                        model=(model_override or custom["model"]).strip(),
                        base_url=custom["base_url"] or None,
                        max_tokens=int(custom["max_tokens"]),
                        thinking_budget=int(custom["thinking_budget"]) or None,
                        use_raw_http=True,
                    )
                openai_settings = LLMSettings(
                    api_key=custom["api_key"],
                    base_url=custom["base_url"],
                    model=custom["model"],
                    reasoning_effort=custom["reasoning_effort"],
                    max_tokens=custom["max_tokens"],
                    wire_api=custom["wire_api"],
                )
            else:
                openai_settings = load_llm_settings()
        except Exception as exc:
            logger.warning("OpenAI adapter 构造失败: %s", exc)
            return None
        if model_override:
            openai_settings = openai_settings.__class__(
                api_key=openai_settings.api_key,
                base_url=openai_settings.base_url,
                model=model_override,
                reasoning_effort=openai_settings.reasoning_effort,
                max_tokens=openai_settings.max_tokens,
                wire_api=openai_settings.wire_api,
            )
        if not openai_settings.api_key:
            return None
        return OpenAIAdapter(settings=openai_settings)

    logger.warning("未知的 LLM provider '%s'，已忽略", provider)
    return None


def create_llm_adapter(
    config: AppConfig,
    model_override: str | None = None,
) -> LLMAdapter:
    """
    根据配置创建 LLM 适配器。

    支持的 Provider：
      - openai:    OpenAI / 兼容 API（默认）
      - anthropic: Anthropic Claude

    如配置了 agent.fallback_providers，则用 FallbackLLMAdapter 依次包裹。
    """
    primary_provider = get_llm_provider()
    primary = build_provider_adapter(primary_provider, model_override=model_override)
    if primary is None:
        if primary_provider == "anthropic":
            raise RuntimeError("使用 Anthropic 需要设置 ANTHROPIC_API_KEY")
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
    config: AppConfig,
    model_override: str | None = None,
) -> LLMAdapter:
    """Create an LLM for a websocket session while tolerating older test stubs."""
    try:
        return create_llm_adapter(config, model_override=model_override)
    except TypeError:
        return create_llm_adapter(config)
