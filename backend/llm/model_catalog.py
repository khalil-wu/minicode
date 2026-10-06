"""Exact production model records shared by configuration and adapters."""

from __future__ import annotations

from dataclasses import dataclass


MODEL_CONTEXT_WINDOW_DEFAULT = 1_000_000


@dataclass(frozen=True)
class ResponsesModelCatalogEntry:
    context_window: int
    max_context_window: int
    reasoning_effort_levels: tuple[str, ...]
    default_reasoning_effort: str
    default_reasoning_summary: str
    multi_agent_reasoning_effort: str = ""

    @property
    def reasoning_effort_wire_map(self) -> dict[str, str]:
        if "ultra" not in self.reasoning_effort_levels:
            return {}
        # ModelInfo::resolve_reasoning_effort treats Ultra as collaboration
        # mode, then selects its model-owned ordinary inference effort.
        wire = self.multi_agent_reasoning_effort or next(
            (level for level in ("max", *reversed(self.reasoning_effort_levels)) if level != "ultra" and level in self.reasoning_effort_levels)
        )
        return {"ultra": wire}


_RESPONSES_MODEL_CATALOG: dict[str, ResponsesModelCatalogEntry] = {
    # Codex models-manager/models.json at 822e58cc (2026-10-06).
    # Keep MiniCode's application context default separate from the catalog limit.
    "gpt-6.1-sol": ResponsesModelCatalogEntry(
        context_window=MODEL_CONTEXT_WINDOW_DEFAULT,
        max_context_window=872_000,
        reasoning_effort_levels=("low", "medium", "high", "xhigh", "max", "ultra"),
        default_reasoning_effort="low",
        default_reasoning_summary="none",
        multi_agent_reasoning_effort="xhigh",
    ),
    "gpt-6-sol": ResponsesModelCatalogEntry(
        context_window=MODEL_CONTEXT_WINDOW_DEFAULT,
        max_context_window=872_000,
        reasoning_effort_levels=("low", "medium", "high", "xhigh", "max", "ultra"),
        default_reasoning_effort="medium",
        default_reasoning_summary="none",
    ),
    # https://developers.openai.com/api/docs/models/gpt-6-luna
    # The application default is independent of the provider's published maximum.
    "gpt-6-luna": ResponsesModelCatalogEntry(
        context_window=MODEL_CONTEXT_WINDOW_DEFAULT,
        max_context_window=1_050_000,
        reasoning_effort_levels=("low", "medium", "high", "xhigh", "max"),
        default_reasoning_effort="medium",
        default_reasoning_summary="none",
    ),
    "gpt-6-astra": ResponsesModelCatalogEntry(
        context_window=MODEL_CONTEXT_WINDOW_DEFAULT,
        max_context_window=1_050_000,
        reasoning_effort_levels=("low", "medium", "high", "xhigh", "max", "ultra"),
        default_reasoning_effort="low",
        default_reasoning_summary="none",
        multi_agent_reasoning_effort="xhigh",
    ),
    "gpt-5.6-sol": ResponsesModelCatalogEntry(
        context_window=MODEL_CONTEXT_WINDOW_DEFAULT,
        max_context_window=272_000,
        reasoning_effort_levels=("low", "medium", "high", "xhigh", "max", "ultra"),
        default_reasoning_effort="low",
        default_reasoning_summary="none",
    ),
    "gpt-5.6-terra": ResponsesModelCatalogEntry(
        context_window=MODEL_CONTEXT_WINDOW_DEFAULT,
        max_context_window=272_000,
        reasoning_effort_levels=("low", "medium", "high", "xhigh", "max", "ultra"),
        default_reasoning_effort="medium",
        default_reasoning_summary="none",
    ),
    "gpt-5.6-luna": ResponsesModelCatalogEntry(
        context_window=MODEL_CONTEXT_WINDOW_DEFAULT,
        max_context_window=272_000,
        reasoning_effort_levels=("low", "medium", "high", "xhigh", "max"),
        default_reasoning_effort="medium",
        default_reasoning_summary="none",
    ),
    "gpt-5.5": ResponsesModelCatalogEntry(
        context_window=MODEL_CONTEXT_WINDOW_DEFAULT,
        max_context_window=272_000,
        reasoning_effort_levels=("low", "medium", "high", "xhigh"),
        default_reasoning_effort="medium",
        default_reasoning_summary="none",
    ),
    "gpt-5.4": ResponsesModelCatalogEntry(
        context_window=MODEL_CONTEXT_WINDOW_DEFAULT,
        max_context_window=1_000_000,
        reasoning_effort_levels=("low", "medium", "high", "xhigh"),
        default_reasoning_effort="medium",
        default_reasoning_summary="none",
    ),
    "gpt-5.4-mini": ResponsesModelCatalogEntry(
        context_window=MODEL_CONTEXT_WINDOW_DEFAULT,
        max_context_window=272_000,
        reasoning_effort_levels=("low", "medium", "high", "xhigh"),
        default_reasoning_effort="medium",
        default_reasoning_summary="none",
    ),
    "gpt-5.2": ResponsesModelCatalogEntry(
        context_window=MODEL_CONTEXT_WINDOW_DEFAULT,
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
    "MODEL_CONTEXT_WINDOW_DEFAULT",
    "ResponsesModelCatalogEntry",
    "responses_model_catalog_entry",
    "terminal_model_id",
]
