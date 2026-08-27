"""Reasoning-effort capability resolution and validation."""
from __future__ import annotations

from backend.llm.model_catalog import responses_model_catalog_entry


def _known_reasoning_effort_levels(model: str, wire_api: str) -> tuple[str, ...]:
    if str(wire_api or "").strip().lower() != "responses":
        return ()
    entry = responses_model_catalog_entry(model)
    return entry.reasoning_effort_levels if entry is not None else ()


def default_reasoning_effort(
    model: str,
    wire_api: str,
    declared_default: object = None,
) -> str:
    """Resolve provider metadata first, then the exact Codex model default."""

    if str(wire_api or "").strip().lower() != "responses":
        return ""
    declared = str(declared_default or "").strip().lower()
    if declared:
        return declared
    entry = responses_model_catalog_entry(model)
    return entry.default_reasoning_effort if entry is not None else ""


def reasoning_effort_levels(
    model: str,
    wire_api: str,
    declared_levels: object = None,
) -> tuple[str, ...]:
    """Resolve provider-declared levels, then exact known-model metadata."""
    if isinstance(declared_levels, (list, tuple)):
        levels = tuple(
            level
            for item in declared_levels
            if (level := str(item or "").strip().lower())
        )
        if levels:
            return tuple(dict.fromkeys(levels))

    if str(wire_api or "").strip().lower() != "responses":
        return ()
    return _known_reasoning_effort_levels(model, wire_api)


def normalize_reasoning_effort(
    model: str,
    wire_api: str,
    effort: str,
    declared_levels: object = None,
    declared_default: object = None,
) -> str:
    """Accept an effort only when resolved capabilities allow it."""
    normalized = str(effort or "").strip().lower()
    levels = reasoning_effort_levels(model, wire_api, declared_levels)
    if not levels:
        return ""
    if not normalized:
        default = default_reasoning_effort(model, wire_api, declared_default)
        return default if default in levels else ""
    return normalized if normalized in levels else ""
