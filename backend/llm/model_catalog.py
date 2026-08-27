"""Exact production model records shared by configuration and adapters."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ResponsesModelCatalogEntry:
    context_window: int
    max_context_window: int
    reasoning_effort_levels: tuple[str, ...]
    default_reasoning_effort: str
    default_reasoning_summary: str


_RESPONSES_MODEL_CATALOG: dict[str, ResponsesModelCatalogEntry] = {
    "gpt-5.6-sol": ResponsesModelCatalogEntry(
        context_window=272_000,
        max_context_window=272_000,
        reasoning_effort_levels=("low", "medium", "high", "xhigh", "max", "ultra"),
        default_reasoning_effort="low",
        default_reasoning_summary="none",
    ),
    "gpt-5.6-terra": ResponsesModelCatalogEntry(
        context_window=272_000,
        max_context_window=272_000,
        reasoning_effort_levels=("low", "medium", "high", "xhigh", "max", "ultra"),
        default_reasoning_effort="medium",
        default_reasoning_summary="none",
    ),
    "gpt-5.6-luna": ResponsesModelCatalogEntry(
        context_window=272_000,
        max_context_window=272_000,
        reasoning_effort_levels=("low", "medium", "high", "xhigh", "max"),
        default_reasoning_effort="medium",
        default_reasoning_summary="none",
    ),
    "gpt-5.5": ResponsesModelCatalogEntry(
        context_window=272_000,
        max_context_window=272_000,
        reasoning_effort_levels=("low", "medium", "high", "xhigh"),
        default_reasoning_effort="medium",
        default_reasoning_summary="none",
    ),
    "gpt-5.4": ResponsesModelCatalogEntry(
        context_window=272_000,
        max_context_window=1_000_000,
        reasoning_effort_levels=("low", "medium", "high", "xhigh"),
        default_reasoning_effort="medium",
        default_reasoning_summary="none",
    ),
    "gpt-5.4-mini": ResponsesModelCatalogEntry(
        context_window=272_000,
        max_context_window=272_000,
        reasoning_effort_levels=("low", "medium", "high", "xhigh"),
        default_reasoning_effort="medium",
        default_reasoning_summary="none",
    ),
    "gpt-5.2": ResponsesModelCatalogEntry(
        context_window=272_000,
        max_context_window=272_000,
        reasoning_effort_levels=("low", "medium", "high", "xhigh"),
        default_reasoning_effort="medium",
        default_reasoning_summary="auto",
    ),
}


def terminal_model_id(model: str) -> str:
    return str(model or "").strip().lower().rsplit("/", 1)[-1]


def responses_model_catalog_entry(
    model: str,
) -> ResponsesModelCatalogEntry | None:
    return _RESPONSES_MODEL_CATALOG.get(terminal_model_id(model))


__all__ = [
    "ResponsesModelCatalogEntry",
    "responses_model_catalog_entry",
    "terminal_model_id",
]
