from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
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


# ---------------------------------------------------------------------------
# Unified terminal record for tool execution (plan §9.4 ToolOutcome)
# ---------------------------------------------------------------------------

class ToolOutcomeStatus(str, Enum):
    """Single source of truth for tool-call terminal state.

    Every code path that finalises a tool call—success, exception, timeout,
    cancellation, pre-execution rejection, or permission block—MUST produce
    a ToolOutcome with one of these statuses.  This replaces the ad-hoc
    string fields (``result.status``, ``result.is_error``) that were spread
    across ``tool_execution.py``.
    """

    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    BLOCKED = "blocked"       # pre-execution guard rejected the call
    REJECTED = "rejected"     # permission policy denied the call

    @property
    def is_terminal(self) -> bool:
        return True

    @property
    def is_error(self) -> bool:
        return self in {
            ToolOutcomeStatus.FAILED,
            ToolOutcomeStatus.CANCELLED,
            ToolOutcomeStatus.TIMEOUT,
            ToolOutcomeStatus.BLOCKED,
            ToolOutcomeStatus.REJECTED,
        }


@dataclass(frozen=True)
class ToolOutcome:
    """Unified terminal record for a single tool call.

    This is the **only** type that should leave the tool-execution layer.
    Downstream consumers (event emitter, context builder, Inspector, Inspector
    aggregation) read from this record rather than probing ``ToolResult.status``
    or ``ToolResult.is_error`` directly.

    Design notes
    ------------
    * ``call_id`` — the model-assigned tool-call ID (matches ``ToolCallEvent.id``).
    * ``status`` — terminal state enum (never a free-form string).
    * ``content`` — compact text injected into agent context (same as
      ``ToolResult.content``).
    * ``error`` — human-readable error message when ``status.is_error``;
      empty string otherwise.
    * ``result_kind`` — semantic category (``edit``, ``search``, ``exec``, …)
      derived from ``ToolProjectionRegistry``.
    * ``activity_kind`` — UI grouping hint (``tool``, ``context``, ``plan``…).
    * ``panel_hint`` — which desktop panel should surface this result
      (``inspector``, ``diff``, ``subagents``, ``terminal``).
    * ``side_effect_kind`` — ``none`` / ``workspace`` / ``external`` / ``destructive``.
    * ``idempotent`` — whether the call can be safely retried.
    * ``started_at`` / ``completed_at`` — epoch milliseconds for timing.
    """

    call_id: str
    tool_name: str
    status: ToolOutcomeStatus
    content: str = ""
    error: str = ""
    result_kind: str = ""
    activity_kind: str = "tool"
    panel_hint: str = "inspector"
    side_effect_kind: str = "none"
    idempotent: bool | None = None
    started_at: int = 0
    completed_at: int = 0
    artifact_id: str | None = None
    artifact_preview: str | None = None
    display_summary: str | None = None
    source_url: str | None = None
    evidence_type: str | None = None
    provider: str | None = None
    provider_error_type: str | None = None
    duration_ms: int | None = None
    requires_attention: bool = False

    # -- factory helpers -------------------------------------------------

    @classmethod
    def from_result(
        cls,
        *,
        call_id: str,
        tool_name: str,
        result: Any,  # ToolResult — late-typed to avoid circular import
        status: ToolOutcomeStatus | None = None,
        result_kind: str = "",
        activity_kind: str = "tool",
        panel_hint: str = "inspector",
        side_effect_kind: str = "none",
        idempotent: bool | None = None,
        started_at: int = 0,
        completed_at: int = 0,
        requires_attention: bool = False,
    ) -> ToolOutcome:
        """Build a ToolOutcome from a legacy ToolResult.

        If ``status`` is None it is derived from ``result.is_error`` and
        ``result.status`` so that existing call-sites get a correct terminal
        state without changing their logic.
        """
        if status is None:
            rs = getattr(result, "status", None) or ""
            is_err = getattr(result, "is_error", False)
            if rs == "timeout":
                status = ToolOutcomeStatus.TIMEOUT
            elif rs == "blocked":
                status = ToolOutcomeStatus.BLOCKED
            elif rs in {"failed", "error"} or is_err:
                status = ToolOutcomeStatus.FAILED
            elif rs in {"cancelled", "canceled"}:
                status = ToolOutcomeStatus.CANCELLED
            else:
                status = ToolOutcomeStatus.COMPLETED

        return cls(
            call_id=call_id,
            tool_name=tool_name,
            status=status,
            content=getattr(result, "content", "") or "",
            error=(getattr(result, "content", "") or "") if status.is_error else "",
            result_kind=result_kind,
            activity_kind=activity_kind,
            panel_hint=panel_hint,
            side_effect_kind=side_effect_kind,
            idempotent=idempotent,
            started_at=started_at,
            completed_at=completed_at,
            artifact_id=getattr(result, "artifact_id", None),
            artifact_preview=getattr(result, "artifact_preview", None),
            display_summary=getattr(result, "display_summary", None),
            source_url=getattr(result, "source_url", None),
            evidence_type=getattr(result, "evidence_type", None),
            provider=getattr(result, "provider", None),
            provider_error_type=getattr(result, "provider_error_type", None),
            duration_ms=getattr(result, "duration_ms", None),
            requires_attention=requires_attention,
        )

    @classmethod
    def cancelled(
        cls,
        *,
        call_id: str,
        tool_name: str,
        reason: str = "Cancelled: parallel tool call errored.",
        started_at: int = 0,
        completed_at: int = 0,
    ) -> ToolOutcome:
        return cls(
            call_id=call_id,
            tool_name=tool_name,
            status=ToolOutcomeStatus.CANCELLED,
            content=reason,
            error=reason,
            started_at=started_at,
            completed_at=completed_at,
            requires_attention=True,
        )

    @classmethod
    def blocked(
        cls,
        *,
        call_id: str,
        tool_name: str,
        reason: str,
        source: str = "guard",
        started_at: int = 0,
        completed_at: int = 0,
    ) -> ToolOutcome:
        return cls(
            call_id=call_id,
            tool_name=tool_name,
            status=ToolOutcomeStatus.BLOCKED,
            content=reason,
            error=reason,
            started_at=started_at,
            completed_at=completed_at,
            requires_attention=True,
        )

    def to_dict(self) -> dict[str, object]:
        d = asdict(self)
        d["status"] = self.status.value
        return d


# ---------------------------------------------------------------------------
# Unified pre-permission decision (plan §10.2 PermissionDecision)
# ---------------------------------------------------------------------------

class PermissionDecisionKind(str, Enum):
    """Outcome of a pre-execution permission check."""

    ALLOW = "allow"
    CONFIRM = "confirm"
    DENY = "deny"


@dataclass(frozen=True)
class PermissionDecision:
    """Unified output of every pre-permission guard.

    Replaces the ad-hoc tuple ``(should_block: bool, reason: str)`` that was
    returned independently by:

    * coordinator tool-block check
    * tool-disabled check
    * command-restriction check
    * permission policy (``PermissionChecker``)

    Every guard now returns a ``PermissionDecision``.  The executor consults
    ``decision.kind``: ``allow`` → proceed; ``confirm`` → ask user;
    ``deny`` → skip and emit a ``ToolOutcome(status=REJECTED)``.
    """

    kind: PermissionDecisionKind
    reason: str = ""
    source: str = "policy"  # coordinator | tool_disabled | command_restricted | policy
    ui_hint: str = ""

    @staticmethod
    def allow(source: str = "policy") -> PermissionDecision:
        return PermissionDecision(kind=PermissionDecisionKind.ALLOW, source=source)

    @staticmethod
    def confirm(reason: str, source: str = "policy", ui_hint: str = "") -> PermissionDecision:
        return PermissionDecision(
            kind=PermissionDecisionKind.CONFIRM,
            reason=reason,
            source=source,
            ui_hint=ui_hint,
        )

    @staticmethod
    def deny(reason: str, source: str = "policy", ui_hint: str = "") -> PermissionDecision:
        return PermissionDecision(
            kind=PermissionDecisionKind.DENY,
            reason=reason,
            source=source,
            ui_hint=ui_hint,
        )

    @property
    def is_allow(self) -> bool:
        return self.kind is PermissionDecisionKind.ALLOW

    @property
    def is_confirm(self) -> bool:
        return self.kind is PermissionDecisionKind.CONFIRM

    @property
    def is_deny(self) -> bool:
        return self.kind is PermissionDecisionKind.DENY


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
