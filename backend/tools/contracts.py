from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


ToolExposure = Literal["core", "deferred", "hidden"]


@dataclass(frozen=True)
class ToolSchemaView:
    """Unified per-tool view derived from one source of truth.

    Consolidates the direct/deferred/hidden decision and the model- vs
    runtime-facing split that was previously spread across tool_spec_for,
    ToolsetPolicy, and DeferredToolCatalog. ``schema`` is the model-facing
    function schema, present for any non-hidden tool; ``direct`` says whether it
    belongs in this turn's direct tool list (vs. discoverable via tool_search).
    ``runtime_metadata`` carries permission/UI hints that must never leak into
    ``schema``.
    """

    name: str
    exposure: ToolExposure
    schema: dict[str, Any] | None
    direct: bool = False
    search_hint: str = ""
    short_description: str = ""
    runtime_metadata: dict[str, Any] = field(default_factory=dict)


RepairStatus = Literal[
    "unchanged",
    "repaired",
    "needs_user_input",
    "needs_model_generation",
    "routing_correction",
    "blocked",
]
ToolProjectionKind = Literal["silent", "status", "warning", "error", "approval"]


@dataclass(frozen=True)
class ToolSpec:
    """Runtime metadata used by tool registration and execution.

    The JSON schema tells the model the shape of a tool. ToolSpec tells the
    runtime how to expose, repair, route, and project that tool without
    encoding business-domain special cases.
    """

    name: str
    capability: str = ""
    toolset: str = "core"
    exposure: ToolExposure = "core"
    always_load: bool = False  # force direct visibility even when deferred
    required_args: tuple[str, ...] = ()
    arg_roles: dict[str, str] | None = None
    arg_sources: dict[str, tuple[str, ...]] | None = None
    repair_policy: dict[str, str] | None = None
    accepted_resource_types: tuple[str, ...] = ()
    rejected_resource_types: tuple[str, ...] = ()
    default_args: dict[str, Any] | None = None
    empty_args_policy: str = "block"
    blocked_guidance: str = ""

    def role_for(self, arg_name: str) -> str:
        return (self.arg_roles or {}).get(arg_name, "")

    def policy_for(self, arg_name: str) -> str:
        explicit = (self.repair_policy or {}).get(arg_name, "")
        if explicit:
            return explicit
        role = self.role_for(arg_name)
        if role == "generated_content":
            return "needs_model_generation"
        if role in {
            "workspace_file",
            "workspace_output_path",
            "search_query",
            "latest_url",
            "latest_artifact",
        }:
            return "resource_resolver"
        if role in {"control", "write_guard"}:
            return "runtime_control"
        if role == "explicit_document_source":
            return "routing_correction"
        return ""


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
class RepairOutcome:
    status: RepairStatus
    user_summary: str = ""
    model_observation: str = ""
    developer_detail: str = ""
    confidence: float = 0.0


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
