"""Exact production model records shared by configuration and adapters."""

from __future__ import annotations

from dataclasses import dataclass, replace


# models-manager/src/model_info.rs::model_info_from_slug
MODEL_CONTEXT_WINDOW_DEFAULT = 272_000


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
    # Codex models-manager/models.json at 970111f6.
    "gpt-6.1-sol": ResponsesModelCatalogEntry(
        context_window=272_000,
        max_context_window=872_000,
        reasoning_effort_levels=("low", "medium", "high", "xhigh", "max", "ultra"),
        default_reasoning_effort="low",
        default_reasoning_summary="none",
        multi_agent_reasoning_effort="xhigh",
    ),
    "gpt-6-sol": ResponsesModelCatalogEntry(
        context_window=272_000,
        max_context_window=872_000,
        reasoning_effort_levels=("low", "medium", "high", "xhigh", "max", "ultra"),
        default_reasoning_effort="medium",
        default_reasoning_summary="none",
    ),
    "gpt-6-luna": ResponsesModelCatalogEntry(
        context_window=272_000,
        max_context_window=872_000,
        reasoning_effort_levels=("low", "medium", "high", "xhigh", "max"),
        default_reasoning_effort="medium",
        default_reasoning_summary="none",
    ),
    "gpt-6-astra": ResponsesModelCatalogEntry(
        context_window=272_000,
        max_context_window=872_000,
        reasoning_effort_levels=("low", "medium", "high", "xhigh", "max", "ultra"),
        default_reasoning_effort="low",
        default_reasoning_summary="none",
        multi_agent_reasoning_effort="xhigh",
    ),
    "gpt-5.6-sol": ResponsesModelCatalogEntry(
        context_window=272_000,
        max_context_window=872_000,
        reasoning_effort_levels=("low", "medium", "high", "xhigh", "max", "ultra"),
        default_reasoning_effort="low",
        default_reasoning_summary="none",
    ),
    "gpt-5.6-terra": ResponsesModelCatalogEntry(
        context_window=272_000,
        max_context_window=872_000,
        reasoning_effort_levels=("low", "medium", "high", "xhigh", "max", "ultra"),
        default_reasoning_effort="medium",
        default_reasoning_summary="none",
    ),
    "gpt-5.6-luna": ResponsesModelCatalogEntry(
        context_window=272_000,
        max_context_window=872_000,
        reasoning_effort_levels=("low", "medium", "high", "xhigh", "max"),
        default_reasoning_effort="medium",
        default_reasoning_summary="none",
    ),
    "gpt-daybreak-blue-latest": ResponsesModelCatalogEntry(
        context_window=272_000,
        max_context_window=872_000,
        reasoning_effort_levels=("low", "medium", "high", "xhigh", "max", "ultra"),
        default_reasoning_effort="low",
        default_reasoning_summary="none",
    ),
    "gpt-daybreak-red-latest": ResponsesModelCatalogEntry(
        context_window=372_000,
        max_context_window=372_000,
        reasoning_effort_levels=("low", "medium", "high", "xhigh", "max", "ultra"),
        default_reasoning_effort="medium",
        default_reasoning_summary="none",
    ),
    "codex-auto-review": ResponsesModelCatalogEntry(
        context_window=272_000,
        max_context_window=872_000,
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


def responses_model_catalog_exact(model: str) -> bool:
    """True only for a catalog id or its alias, never for a prefix lookalike."""
    from backend.llm.bedrock import MODEL_ALIASES
    model_id = terminal_model_id(model)
    return MODEL_ALIASES.get(model_id, model_id) in _RESPONSES_MODEL_CATALOG


def responses_model_catalog_entry(
    model: str,
) -> ResponsesModelCatalogEntry | None:
    from backend.llm.bedrock import MODEL_ALIASES
    model_id = terminal_model_id(model)
    source_id = MODEL_ALIASES.get(model_id, model_id)
    entry = _RESPONSES_MODEL_CATALOG.get(source_id)
    if entry is None:
        matches = (name for name in _RESPONSES_MODEL_CATALOG if source_id.startswith(name))
        name = max(matches, key=len, default="")
        entry = _RESPONSES_MODEL_CATALOG.get(name)
    if model_id in MODEL_ALIASES and source_id == "gpt-5.5" and entry is not None:
        return replace(entry, context_window=272000, max_context_window=272000)
    return entry


__all__ = [
    "MODEL_CONTEXT_WINDOW_DEFAULT",
    "ResponsesModelCatalogEntry",
    "responses_model_catalog_entry",
    "responses_model_catalog_exact",
    "terminal_model_id",
]
