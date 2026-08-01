from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


ToolExposure = Literal["core", "deferred", "hidden"]


@dataclass(frozen=True)
class ToolSchemaView:
    """Unified per-tool view derived from one source of truth.

    Consolidates the direct/deferred/hidden decision and the model- vs
    runtime-facing split that was previously spread across tool_spec_for,
    ToolsetPolicy, and DeferredToolCatalog. ``schema`` is the materialized
    model-facing function schema only when the tool belongs in this turn's
    direct tools array; deferred tools expose a lightweight catalog record and
    materialize their schema only through tool_describe. ``runtime_metadata``
    carries permission/UI hints that must never leak into ``schema``.
    """

    name: str
    exposure: ToolExposure
    schema: dict[str, Any] | None
    direct: bool = False
    schema_available: bool = False
    catalog_text: str = ""
    search_hint: str = ""
    short_description: str = ""
    runtime_metadata: dict[str, Any] = field(default_factory=dict)


ToolProjectionKind = Literal["silent", "status", "warning", "error", "approval"]


@dataclass(frozen=True)
class ToolSpec:
    """Runtime metadata used by tool registration and exposure.

    The JSON schema tells the model the shape of a tool. ToolSpec tells the
    runtime how to expose it.
    """

    name: str
    capability: str = ""
    toolset: str = "core"
    exposure: ToolExposure = "core"
    always_load: bool = False  # force direct visibility even when deferred
    required_args: tuple[str, ...] = ()


@dataclass(frozen=True)
class SearchPlan:
    raw_query: str
    normalized_query: str
    required_date: str | None = None
    timezone: str = ""
    freshness_window: str = "stable"
    preferred_source_categories: tuple[str, ...] = ()
    reject_before: str | None = None


@dataclass(frozen=True)
class EvidenceRecord:
    source_url: str = ""
    source_name: str = ""
    retrieved_at: str = ""
    published_at: str | None = None
    valid_for_date: str | None = None
    evidence_type: str = "candidate"
    extracted_facts: dict[str, Any] = field(default_factory=dict)
    authority_score: float = 0.0
    freshness_score: float = 0.0
    confidence: float = 0.0
    tool_call_id: str = ""
    tool_name: str = ""


@dataclass(frozen=True)
class ToolIssue:
    error_kind: str
    user_summary: str
    developer_detail: str
    recoverable: bool = True
    projection: ToolProjectionKind = "error"
    model_observation: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)
