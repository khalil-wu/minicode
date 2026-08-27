from __future__ import annotations

import math
import re
from typing import Any


_NON_NEGATIVE_INTEGER_RE = re.compile(r"^(?:0|[1-9][0-9]*)$")


def _strict_usage_int(value: Any, default: int = 0) -> int:
    """Accept provider counters only when they are exact non-negative integers."""
    if isinstance(value, bool) or value is None:
        return default
    if isinstance(value, int):
        return value if value >= 0 else default
    if isinstance(value, float):
        return int(value) if math.isfinite(value) and value >= 0 and value.is_integer() else default
    if isinstance(value, str):
        stripped = value.strip()
        return int(stripped) if _NON_NEGATIVE_INTEGER_RE.fullmatch(stripped) else default
    return default


def _get_usage_field(usage_obj: Any, name: str, default: int = 0) -> int:
    if usage_obj is None:
        return default
    if isinstance(usage_obj, dict):
        value = usage_obj.get(name, default)
    else:
        value = getattr(usage_obj, name, default)
    return _strict_usage_int(value, default)


def _get_usage_cost_usd(usage_obj: Any) -> float:
    """Read an explicit provider/gateway cost without inventing model prices."""
    if usage_obj is None:
        return 0.0
    for name in ("cost_usd", "total_cost_usd", "cost", "total_cost"):
        value = usage_obj.get(name) if isinstance(usage_obj, dict) else getattr(usage_obj, name, None)
        if isinstance(value, bool):
            continue
        try:
            parsed = float(value or 0.0)
        except (TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(parsed) and parsed > 0:
            return parsed
    return 0.0


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


def _get_cache_creation_prompt_tokens(usage_obj: Any) -> int:
    """Read provider-reported prompt-cache writes when a gateway exposes them."""
    direct = _first_usage_field(
        usage_obj,
        "cache_creation_input_tokens",
        "cache_creation_tokens",
        "cache_write_input_tokens",
        "cache_write_tokens",
        "prompt_cache_write_tokens",
        "cache_write",
    )
    if direct:
        return direct
    details = None
    if isinstance(usage_obj, dict):
        details = usage_obj.get("prompt_tokens_details") or usage_obj.get("input_tokens_details")
    elif usage_obj is not None:
        details = getattr(usage_obj, "prompt_tokens_details", None) or getattr(usage_obj, "input_tokens_details", None)
    return _first_usage_field(
        details,
        "cache_creation_input_tokens",
        "cache_creation_tokens",
        "cache_write_tokens",
        "cache_write",
    )


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
        "cache_creation_input_tokens",
        "cache_write_tokens",
    )
    raw: dict[str, Any] = {}
    for field in fields:
        value = _get_usage_field(usage_obj, field, 0)
        if value:
            raw[field] = value
    cached = _get_cached_prompt_tokens(usage_obj)
    if cached:
        raw["cached_prompt_tokens"] = cached
    cache_write = _get_cache_creation_prompt_tokens(usage_obj)
    if cache_write:
        raw["cache_creation_input_tokens"] = cache_write
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
