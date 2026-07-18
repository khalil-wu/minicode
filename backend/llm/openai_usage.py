from __future__ import annotations

from typing import Any


def _get_usage_field(usage_obj: Any, name: str, default: int = 0) -> int:
    if usage_obj is None:
        return default
    if isinstance(usage_obj, dict):
        value = usage_obj.get(name, default)
    else:
        value = getattr(usage_obj, name, default)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return default


def _first_usage_field(usage_obj: Any, *names: str) -> int:
    for name in names:
        value = _get_usage_field(usage_obj, name, 0)
        if value:
            return value
    return 0


def _get_chat_prompt_tokens(usage_obj: Any) -> int:
    direct = _first_usage_field(usage_obj, "prompt_tokens", "input_tokens")
    if direct:
        return direct
    hit = _get_usage_field(usage_obj, "prompt_cache_hit_tokens", 0)
    miss = _get_usage_field(usage_obj, "prompt_cache_miss_tokens", 0)
    return hit + miss if hit or miss else 0


def _get_cached_prompt_tokens(usage_obj: Any) -> int:
    direct = _first_usage_field(
        usage_obj,
        "cached_prompt_tokens",
        "prompt_cache_hit_tokens",
        "cache_read_input_tokens",
        "cache_read_tokens",
    )
    if direct:
        return direct
    details = None
    if isinstance(usage_obj, dict):
        details = usage_obj.get("prompt_tokens_details") or usage_obj.get("input_tokens_details")
    elif usage_obj is not None:
        details = getattr(usage_obj, "prompt_tokens_details", None) or getattr(usage_obj, "input_tokens_details", None)
    return _get_usage_field(details, "cached_tokens", 0)


def _get_reasoning_output_tokens(usage_obj: Any) -> int:
    direct = _get_usage_field(usage_obj, "reasoning_output_tokens", 0)
    if direct:
        return direct
    details = None
    if isinstance(usage_obj, dict):
        details = usage_obj.get("completion_tokens_details") or usage_obj.get("output_tokens_details")
    elif usage_obj is not None:
        details = getattr(usage_obj, "completion_tokens_details", None) or getattr(usage_obj, "output_tokens_details", None)
    return _get_usage_field(details, "reasoning_tokens", 0)


def _raw_usage_metadata(usage_obj: Any) -> dict[str, Any]:
    if not usage_obj:
        return {}
    fields = (
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "input_tokens",
        "output_tokens",
        "prompt_cache_hit_tokens",
        "prompt_cache_miss_tokens",
    )
    raw: dict[str, Any] = {}
    for field in fields:
        value = _get_usage_field(usage_obj, field, 0)
        if value:
            raw[field] = value
    cached = _get_cached_prompt_tokens(usage_obj)
    if cached:
        raw["cached_prompt_tokens"] = cached
    reasoning = _get_reasoning_output_tokens(usage_obj)
    if reasoning:
        raw["reasoning_output_tokens"] = reasoning
    return raw


def _raw_text_delta_metadata(
    provider: str,
    *,
    usage_obj: Any = None,
    citations: list[dict[str, Any]] | None = None,
    finish_reason: str = "",
    message_phase: str = "",
) -> dict[str, Any]:
    raw: dict[str, Any] = {}
    usage = _raw_usage_metadata(usage_obj)
    if usage:
        raw["provider"] = provider
        raw["usage"] = usage
    if citations:
        raw["provider"] = provider
        raw["citations"] = citations
    if finish_reason:
        raw["provider"] = provider
        raw["finish_reason"] = finish_reason
    if message_phase:
        raw["provider"] = provider
        raw["message_phase"] = message_phase
    return raw
