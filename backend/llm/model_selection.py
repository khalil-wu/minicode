"""MiniCode model selection, context-budget, and reasoning policy."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from backend.llm.provider_contracts import ReasoningPolicy


REASONING_LEVEL_ORDER = (
    "off", "minimal", "low", "medium", "high", "xhigh", "max"
)


def config_with_model_budget(
    config: Any,
    *,
    model_runtime: Any | None,
    provider: str,
    model: str,
) -> Any:
    """Bind a copied session config to the selected model context window."""
    if model_runtime is None:
        return config
    selected = model_runtime.get_model(provider, model)
    context_window = max(0, int(getattr(selected, "context_window", 0) or 0))
    if context_window <= 0:
        return config
    budget = getattr(config, "token_budget", None)
    if budget is None or context_window < 2:
        return config
    response_reserve = min(
        max(1, int(getattr(budget, "response_reserve", 1) or 1)),
        context_window - 1,
    )
    if (
        int(getattr(budget, "total", 0) or 0) == context_window
        and int(getattr(budget, "response_reserve", 0) or 0) == response_reserve
    ):
        return config
    return replace(
        config,
        token_budget=replace(
            budget,
            total=context_window,
            response_reserve=response_reserve,
        ),
    )


def model_thinking_levels(model: Any, adapter: Any | None = None) -> tuple[str, ...]:
    """Return the exact reasoning values the selected transport can honor."""
    if model is None:
        supported = getattr(adapter, "supported_reasoning_efforts", None)
        if not callable(supported):
            return ()
        return tuple(dict.fromkeys(supported()))
    if not bool(getattr(model, "reasoning", False)):
        return ("off",)
    supported = getattr(adapter, "supported_reasoning_efforts", None)
    adapter_levels = tuple(dict.fromkeys(supported())) if callable(supported) else ()
    if adapter_levels == ("off", "high"):
        return adapter_levels
    mapping = getattr(model, "thinking_level_map", None)
    if isinstance(mapping, Mapping):
        levels: list[str] = []
        for level in REASONING_LEVEL_ORDER:
            has_mapping = level in mapping
            mapped_value = mapping.get(level) if has_mapping else None
            if has_mapping and mapped_value is None:
                continue
            if level in {"xhigh", "max"} and not has_mapping:
                continue
            levels.append(level)
        return tuple(levels)
    declared = tuple(
        str(value).strip().lower()
        for value in (
            getattr(model, "reasoning_effort_levels", ())
            or adapter_levels
            or ()
        )
        if str(value).strip()
    )
    if declared:
        return tuple(dict.fromkeys(declared))
    return ("off", "minimal", "low", "medium", "high")


def clamp_model_thinking_level(requested: Any, available: tuple[str, ...]) -> str:
    value = str(requested or "off").strip().lower()
    if value in available:
        return value
    if not available:
        return ""
    try:
        requested_index = REASONING_LEVEL_ORDER.index(value)
    except ValueError:
        return available[0]
    for index in range(requested_index, len(REASONING_LEVEL_ORDER)):
        candidate = REASONING_LEVEL_ORDER[index]
        if candidate in available:
            return candidate
    for index in range(requested_index - 1, -1, -1):
        candidate = REASONING_LEVEL_ORDER[index]
        if candidate in available:
            return candidate
    return available[0]


def default_model_thinking_level(model: Any, available: tuple[str, ...]) -> str:
    declared = str(getattr(model, "default_reasoning_effort", "") or "").strip().lower()
    if declared in available:
        return declared
    if not bool(getattr(model, "reasoning", False)):
        return ""
    return clamp_model_thinking_level("medium", available)


def apply_model_thinking_level(adapter: Any, model: Any, requested: Any) -> str:
    effective = str(requested or "").strip().lower()
    mapping = getattr(model, "thinking_level_map", None)
    provider_level = effective
    if isinstance(mapping, Mapping) and effective in mapping:
        provider_level = str(mapping.get(effective) or "").strip().lower()
    canonical_levels = model_thinking_levels(model, adapter)
    declared_provider_levels = tuple(
        dict.fromkeys(
            value
            for canonical in canonical_levels
            if (
                value := str(
                    mapping.get(canonical, canonical)
                    if isinstance(mapping, Mapping)
                    else canonical
                ).strip().lower()
            )
            and not (
                canonical == "off"
                and not (
                    isinstance(mapping, Mapping)
                    and canonical in mapping
                    and mapping.get(canonical) is not None
                )
            )
        )
    )
    wire_effort = provider_level
    if effective == "off" and not (
        isinstance(mapping, Mapping)
        and "off" in mapping
        and mapping.get("off") is not None
    ):
        wire_effort = ""
    apply_policy = getattr(adapter, "apply_reasoning_policy", None)
    if not callable(apply_policy):
        raise TypeError("LLM adapter does not implement MiniCode reasoning policy")
    apply_policy(
        ReasoningPolicy(
            level=effective or "off",
            wire_level=wire_effort,
            wire_levels=declared_provider_levels,
        )
    )
    return effective


__all__ = [
    "REASONING_LEVEL_ORDER", "apply_model_thinking_level",
    "clamp_model_thinking_level", "config_with_model_budget",
    "default_model_thinking_level", "model_thinking_levels",
]
