"""Base abstractions for tools exposed to the agent."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
import hashlib
import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from backend.tools.contracts import ToolSpec
    from backend.permissions.context import PermissionContext, ToolExecutionContext


class PermissionLevel(Enum):
    """Permission level required before a tool can run."""

    AUTO = "auto"
    CONFIRM = "confirm"
    DIFF_REVIEW = "diff"
    ALWAYS_DENY = "deny"


@dataclass
class ToolResult:
    """Normalized tool execution result.

    content is the compact text injected into agent context. Oversized outputs
    should be stored as artifacts and represented here by artifact_id plus a
    short preview.
    """

    content: str
    artifact_id: str | None = None
    artifact_preview: str | None = None
    is_error: bool = False
    source_url: str | None = None
    extraction_status: str | None = None
    content_preview: str | None = None
    evidence_type: str | None = None
    display_summary: str | None = None
    result_kind: str | None = None
    limitation: str | None = None
    duration_ms: int | None = None
    status: str | None = None
    provider: str | None = None
    provider_error_type: str | None = None
    display_scope: str | None = None

    def to_context_string(self) -> str:
        """Return the compact representation injected into model context.

        Includes quality metadata and timing so the model can make informed decisions.
        """
        parts: list[str] = [self.content]
        # Metadata line — quality signals + timing
        meta_parts: list[str] = []
        if self.is_error:
            meta_parts.append("status: error")
        if self.evidence_type:
            meta_parts.append(f"evidence: {self.evidence_type}")
        if self.extraction_status:
            meta_parts.append(f"extraction: {self.extraction_status}")
        if self.duration_ms is not None:
            if self.duration_ms >= 1000:
                meta_parts.append(f"wall_time: {self.duration_ms / 1000:.1f}s")
            else:
                meta_parts.append(f"wall_time: {self.duration_ms}ms")
        if self.status and self.status not in ("success", "completed"):
            meta_parts.append(f"status: {self.status}")
        if meta_parts:
            parts.append(f"[{', '.join(meta_parts)}]")
        if self.source_url and self.evidence_type == "fetched":
            parts.append(f"Source: {self.source_url}")
        if self.content_preview:
            parts.append(f"--- content preview ---\n{self.content_preview}")
        elif self.artifact_preview:
            parts.append(f"--- artifact preview ---\n{self.artifact_preview}")
        if self.artifact_id:
            parts.append(
                f"Full result saved as artifact. Use read_artifact('{self.artifact_id}') if more detail is needed."
            )
        return "\n".join(parts)


MAX_TOOL_RESULT_CHARS = 12_000
LEGACY_CONCURRENCY_SAFE_TOOL_NAMES = frozenset({
    "read_file",
    "list_files",
    "grep_files",
    "glob_files",
    "fuzzy_search",
    "git_status",
    "git_diff",
    "git_log",
    "go_to_definition",
    "find_references",
    "read_artifact",
    "web_search",
    "web_fetch",
    "tool_search",
    "tool_describe",
})
TOOL_SIDE_EFFECT_NONE = "none"
TOOL_SIDE_EFFECT_WORKSPACE = "workspace"
TOOL_SIDE_EFFECT_EXTERNAL = "external"
TOOL_SIDE_EFFECT_DESTRUCTIVE = "destructive"
TOOL_SIDE_EFFECT_KINDS = frozenset({
    TOOL_SIDE_EFFECT_NONE,
    TOOL_SIDE_EFFECT_WORKSPACE,
    TOOL_SIDE_EFFECT_EXTERNAL,
    TOOL_SIDE_EFFECT_DESTRUCTIVE,
})


def truncate_tool_result(content: str, max_chars: int = MAX_TOOL_RESULT_CHARS) -> str:
    """Truncate long tool output while keeping head and tail context."""
    if len(content) <= max_chars:
        return content
    head_len = int(max_chars * 0.8)
    tail_len = int(max_chars * 0.1)
    head = content[:head_len]
    tail = content[-tail_len:]
    omitted = len(content) - head_len - tail_len
    return f"{head}\n\n... [{omitted} chars truncated] ...\n\n{tail}"


@dataclass
class ToolSchema:
    """JSON schema definition for an agent tool."""

    name: str
    description: str
    parameters: dict[str, Any]
    strict: bool = False

    def to_openai_tool(self) -> dict[str, Any]:
        """Convert to OpenAI-compatible function-calling format."""
        tool: dict[str, Any] = {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }
        if self.strict:
            tool["function"]["strict"] = True
        return tool

    def to_summary(self) -> str:
        """Return a compact one-line summary for constrained context budgets."""
        return f"- {self.name}: {self.description.split('.')[0]}"

    def with_description(self, description: str) -> "ToolSchema":
        """Return a copy with a model-facing description override."""
        return ToolSchema(
            name=self.name,
            description=description,
            parameters=self.parameters,
            strict=self.strict,
        )

    def with_parameters(self, parameters: dict[str, Any]) -> "ToolSchema":
        """Return a copy with model-facing parameter overrides."""
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters=parameters,
            strict=self.strict,
        )


class BaseTool(ABC):
    """Base class for all tools."""

    name: str
    description: str
    permission: PermissionLevel = PermissionLevel.AUTO
    read_only: bool = False
    # Capability hints (CC parity). Tools override as needed; registry/MCP
    # adapter and ToolSchemaView derive exposure/permission from these instead
    # of hardcoding per-name special cases.
    destructive: bool = False  # irreversible side effects (delete/overwrite/send)
    open_world: bool = False  # reaches systems outside the workspace (network, external state)
    always_load: bool = False  # full schema must appear on turn 1 even under deferred discovery
    should_defer: bool = False  # hidden behind tool_search until explicitly described
    search_hint: str = ""  # extra keywords for tool_search matching (not in the name)
    # Which deferred-tool directories should list this tool. Empty means the
    # tool can still be direct-enabled by a policy but is not invokable through
    # the generic tool_search/tool_call bridge.
    deferred_catalog_scopes: tuple[str, ...] = ("default",)
    # Execution metadata (Phase 4.2). Tools self-declare these instead of the
    # runtime keeping hardcoded per-name tables. None timeout = use the runtime
    # default. mutates_* drive result-cache invalidation and checkpointing.
    timeout_seconds: float | None = None
    mutates_workspace: bool = False  # writes files / workspace state
    mutates_external_state: bool = False  # commits, sends, external side effects
    # Side-effect/idempotency policy. ``None`` means derive from legacy
    # capability hints; subclasses can set these to make retry/prefetch
    # decisions explicit. ``side_effect_kind`` is intentionally coarse because
    # it is used for runtime scheduling and UI diagnostics, not model prompting.
    side_effect_kind: str | None = None
    idempotent: bool | None = None
    # UI/event projection hints. These are non-model-facing and let each tool own
    # its display category while legacy name inference remains the fallback.
    result_kind: str | None = None
    activity_kind: str | None = None
    display_scope: str | None = None
    panel_hint: str | None = None
    display_label: str | None = None
    # Max chars of result content kept inline before the global truncation
    # backstop fires. Tools that already self-bound and store overflow as an
    # artifact (read_file, web_fetch, run_command) set this to None to skip the
    # backstop — double-truncating their compact summary loses head/tail context.
    max_result_chars: int | None = MAX_TOOL_RESULT_CHARS

    def model_description(self) -> str:
        """Compact capability description shown to the model in the tool schema.

        Defaults to ``description``. Kept separate from runtime/UI text so that
        permission, diff, and evidence metadata never leak into the function
        schema the model sees.
        """
        return getattr(self, "description", "") or ""

    def model_schema(self) -> ToolSchema | None:
        """Optional compact schema shown only to the model.

        Override when the runtime/UI schema should stay rich but the model-facing
        schema can be narrower or terser for latency and prompt-cache efficiency.
        """
        return None

    def runtime_description(self) -> str:
        """Human/UI-facing description. Defaults to ``description``."""
        return getattr(self, "description", "") or ""

    def to_runtime_metadata(self) -> dict[str, Any]:
        """Non-model-facing metadata for UI, permission display, and diagnostics.

        Derived from the tool's declared capability hints. The registry attaches
        this alongside the schema instead of stuffing it into the description.
        """
        permission = self.permission
        permission_value = permission.value if isinstance(permission, PermissionLevel) else PermissionLevel.AUTO.value
        metadata = {
            "permission": permission_value,
            "read_only": self.read_only,
            "destructive": self.destructive,
            "open_world": self.open_world,
            "always_load": self.always_load,
            "should_defer": self.should_defer,
            "deferred_catalog_scopes": self.deferred_catalog_scopes,
            "timeout_seconds": self.timeout_seconds,
            "mutates_workspace": self.mutates_workspace,
            "mutates_external_state": self.mutates_external_state,
            "side_effect_kind": self.get_side_effect_kind(),
            "idempotent": self.is_idempotent(),
            "supports_idempotency_key": self.is_idempotent(),
            "max_result_chars": self.max_result_chars,
        }
        metadata.update(self.to_projection_metadata())
        return metadata

    def to_projection_metadata(self) -> dict[str, Any]:
        """Return UI projection hints owned by this tool.

        Empty values are omitted so the projection registry can continue using
        its legacy name-based fallback for tools that have not migrated yet.
        """
        metadata = {
            "result_kind": self.result_kind,
            "activity_kind": self.activity_kind,
            "display_scope": self.display_scope,
            "panel_hint": self.panel_hint,
            "display_label": self.display_label,
        }
        return {key: value for key, value in metadata.items() if value}

    def validate_input(self, args: dict[str, Any] | None = None) -> str:
        """Tool-owned input validation, run before permission/execution.

        Return an empty string when input is acceptable, or a short actionable
        error to block execution (CC's validateInput). The message is surfaced
        as an observation-style tool result so the model can correct the call.
        Defaults to no validation.
        """
        return ""

    def is_read_only(self, args: dict[str, Any] | None = None) -> bool:
        """Return whether this specific invocation only reads state.

        Defaults to the class-level ``read_only`` flag. Tools whose read-only
        status depends on arguments (e.g. ``run_command`` with ``ls`` vs ``rm``)
        override this to classify per call, mirroring CC's per-input
        ``isReadOnly``.
        """
        return self.read_only

    def check_permission(
        self,
        args: dict[str, Any] | None = None,
        context: "PermissionContext | None" = None,
    ) -> "PermissionLevel | None":
        """Tool-owned permission decision for this invocation.

        Return a concrete ``PermissionLevel`` to override the centralized
        policy, or ``None`` to defer to ``PermissionChecker``. This is the
        per-input analogue of CC's ``checkPermissions``; most tools defer.
        """
        return None

    def is_concurrency_safe(self, args: dict[str, Any] | None = None) -> bool:
        """Return whether this tool can safely run alongside other read-only work."""
        if self.mutates_workspace or self.mutates_external_state:
            return False
        if self.get_side_effect_kind(args) != TOOL_SIDE_EFFECT_NONE:
            return False
        try:
            if self.is_read_only(args):
                return True
        except Exception:
            if self.read_only:
                return True
        if self._declares_metadata("read_only"):
            return False
        return self.name in LEGACY_CONCURRENCY_SAFE_TOOL_NAMES

    def get_side_effect_kind(self, args: dict[str, Any] | None = None) -> str:
        """Return the coarse side-effect class for this invocation.

        Tools with argument-dependent behavior can override this method. The
        default derives from legacy hints so existing tools keep their behavior
        while newer tools can declare a single explicit policy.
        """
        declared = str(self.side_effect_kind or "").strip().lower()
        if declared in TOOL_SIDE_EFFECT_KINDS:
            return declared
        if self.destructive:
            return TOOL_SIDE_EFFECT_DESTRUCTIVE
        if self.mutates_external_state:
            return TOOL_SIDE_EFFECT_EXTERNAL
        if self.mutates_workspace:
            return TOOL_SIDE_EFFECT_WORKSPACE
        return TOOL_SIDE_EFFECT_NONE

    def is_idempotent(self, args: dict[str, Any] | None = None) -> bool:
        """Return whether repeating this exact invocation is side-effect safe."""
        if self.idempotent is not None:
            return bool(self.idempotent)
        if self.get_side_effect_kind(args) != TOOL_SIDE_EFFECT_NONE:
            return False
        try:
            if self.is_read_only(args):
                return True
        except Exception:
            if self.read_only:
                return True
        try:
            return bool(self.is_concurrency_safe(args))
        except Exception:
            return bool(getattr(self, "read_only", False))

    def idempotency_key(self, args: dict[str, Any] | None = None) -> str | None:
        """Stable key for deduping/retry diagnostics of idempotent calls."""
        if not self.is_idempotent(args):
            return None
        canonical = json.dumps(args or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
        return f"{self.name}:{digest}"

    def _declares_metadata(self, name: str) -> bool:
        """Return whether this instance or subclass explicitly declares metadata."""
        if name in self.__dict__:
            return True
        for cls in type(self).__mro__:
            if cls is BaseTool:
                break
            if name in cls.__dict__:
                return True
        return False

    @abstractmethod
    def get_schema(self) -> ToolSchema:
        """Return the JSON schema for this tool."""

    def get_spec(self) -> ToolSpec | None:
        """Return runtime repair metadata, when the tool owns it."""
        return None

    @abstractmethod
    async def execute(
        self,
        args: dict[str, Any],
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        """Execute the tool and return a compact, human-readable result."""

    def _error_result(self, message: str) -> ToolResult:
        """Return a standardized error result."""
        return ToolResult(content=f"Error: {message}", is_error=True)

    def _success_result(
        self,
        content: str,
        artifact_id: str | None = None,
        artifact_preview: str | None = None,
    ) -> ToolResult:
        """Return a standardized success result."""
        return ToolResult(
            content=content,
            artifact_id=artifact_id,
            artifact_preview=artifact_preview,
        )
