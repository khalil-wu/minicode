"""Model-specific reasoning-effort support for OpenAI-compatible runtimes."""
from __future__ import annotations

import re


_GPT_5_6_EFFORTS = ("none", "low", "medium", "high", "xhigh", "max")
_GPT_5_2_PLUS_EFFORTS = ("none", "low", "medium", "high", "xhigh")
_GPT_5_CODEX_EFFORTS = ("low", "medium", "high", "xhigh")
_GPT_5_1_EFFORTS = ("none", "low", "medium", "high")
_GPT_5_EFFORTS = ("minimal", "low", "medium", "high")
_O_SERIES_EFFORTS = ("low", "medium", "high")


def normalized_model_id(model: str) -> str:
    value = str(model or "").strip().replace("_", "-").lower()
    return value.rsplit("/", 1)[-1]


def reasoning_effort_levels(
    model: str,
    wire_api: str,
    declared_levels: object = None,
) -> tuple[str, ...]:
    """Prefer provider-declared values, then use the conservative known-model table."""
    if isinstance(declared_levels, (list, tuple)):
        allowed = {"none", "minimal", "low", "medium", "high", "xhigh", "max"}
        levels = tuple(
            level
            for item in declared_levels
            if (level := str(item or "").strip().lower()) in allowed
        )
        if levels:
            return tuple(dict.fromkeys(levels))

    if str(wire_api or "").strip().lower() != "responses":
        return ()

    normalized = normalized_model_id(model)
    if not normalized or normalized.startswith("gpt-image-"):
        return ()
    if any(marker in normalized for marker in ("-chat", "-audio", "-realtime", "-transcribe", "-tts", "-pro")):
        return ()
    if re.match(r"^gpt-5\.6(?:-|$)", normalized):
        return _GPT_5_6_EFFORTS
    if re.match(r"^gpt-5\.3-codex(?:-|$)", normalized):
        return _GPT_5_CODEX_EFFORTS
    if re.match(r"^gpt-5\.(?:[2-5])(?:-|$)", normalized):
        return _GPT_5_2_PLUS_EFFORTS
    if re.match(r"^gpt-5\.1(?:-|$)", normalized):
        return _GPT_5_1_EFFORTS
    if normalized == "gpt-5" or re.match(r"^gpt-5-(?:mini|nano)(?:-|$)", normalized):
        return _GPT_5_EFFORTS
    if normalized == "codex-mini-latest" or re.match(r"^o(?:1|3|4)(?:-|$)", normalized):
        return _O_SERIES_EFFORTS
    return ()


def normalize_reasoning_effort(
    model: str,
    wire_api: str,
    effort: str,
    declared_levels: object = None,
) -> str:
    """Normalize legacy aliases without changing GPT-5.6's real ``max`` value."""
    normalized = str(effort or "").strip().lower()
    levels = reasoning_effort_levels(model, wire_api, declared_levels)
    if not levels:
        return ""
    if normalized in levels:
        return normalized
    if normalized == "max" and "max" not in levels and "xhigh" in levels:
        return "xhigh"
    if normalized in {"max", "xhigh"} and "high" in levels:
        return "high"
    if normalized in {"none", "minimal"}:
        if "none" in levels:
            return "none"
        if "minimal" in levels:
            return "minimal"
        if "low" in levels:
            return "low"
    return normalized
