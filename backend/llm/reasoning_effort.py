"""Provider-declared reasoning-effort validation."""
from __future__ import annotations

def reasoning_effort_levels(
    model: str,
    wire_api: str,
    declared_levels: object = None,
) -> tuple[str, ...]:
    """Return only levels declared by the provider's live model metadata."""
    if isinstance(declared_levels, (list, tuple)):
        allowed = {"none", "minimal", "low", "medium", "high", "xhigh", "max"}
        levels = tuple(
            level
            for item in declared_levels
            if (level := str(item or "").strip().lower()) in allowed
        )
        if levels:
            return tuple(dict.fromkeys(levels))

    return ()


def normalize_reasoning_effort(
    model: str,
    wire_api: str,
    effort: str,
    declared_levels: object = None,
) -> str:
    """Accept an effort only when the provider declared it for this model."""
    normalized = str(effort or "").strip().lower()
    levels = reasoning_effort_levels(model, wire_api, declared_levels)
    if not levels:
        return ""
    return normalized if normalized in levels else ""
