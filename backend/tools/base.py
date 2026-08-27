"""Base abstractions for tools exposed to the agent."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
import hashlib
import json
from typing import TYPE_CHECKING, Any

from jsonschema import SchemaError
from jsonschema.validators import validator_for

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
    # Inline image attachments (base64) for multimodal tool results, e.g. a
    # read_file on a PNG (cc FileReadTool returns an image block). Each item:
    # {"media_type": "image/png", "data": "<base64>"}. Anthropic renders these
    # as image blocks inside the tool_result content; other providers get text.
    images: list[dict[str, str]] = field(default_factory=list)
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
    error_kind: str | None = None
    user_summary: str | None = None
    developer_detail: str | None = None
    recoverable: bool = True
    projection: str | None = None
    model_observation: str | None = None
    output_files: list[dict[str, Any]] = field(default_factory=list)
    superseded_tool_call_ids: list[str] = field(default_factory=list)
    removed_file_paths: list[str] = field(default_factory=list)
    # Identity of the frozen provider-neutral request that produced this
    # result.  It is safe to expose (it is only a SHA-256 digest) and lets
    # approvals, state, journal and projections prove they refer to one
    # authorized request without persisting sensitive arguments again.
    request_digest: str = ""
    # Cancellation/cleanup evidence for side-effecting tools.  This is kept
    # structured all the way through the event and journal projections so a
    # timeout cannot be mistaken for confirmed termination.
    cleanup_receipt: dict[str, Any] = field(default_factory=dict)
    # Private hand-off data for model-facing protocol adapters that reuse a
    # canonical executor.  The normal tool event/context projection does not
    # serialize this field; wrappers may read it only while the nested execute
    # call is still in-process.  Keeping opaque ids and resolved child config
    # here avoids parsing human-readable ToolResult.content or creating a
    # second Agent runtime path merely to obtain a dialect-specific result.
    runtime_metadata: dict[str, Any] = field(default_factory=dict, repr=False)

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
        cleanup = self.cleanup_receipt if isinstance(self.cleanup_receipt, dict) else {}
        if cleanup:
            if cleanup.get("pending") or cleanup.get("manual_recovery_required"):
                meta_parts.append("cleanup: pending/manual recovery required")
            elif cleanup.get("completed"):
                meta_parts.append("cleanup: completed")
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


# MiniCode persists tool results above DEFAULT_MAX_RESULT_SIZE_CHARS=50_000
# chars (toolLimits.ts) — this inline ceiling is the same persist trigger, kept
# as the shared tool-output contract.
MAX_TOOL_RESULT_LINES = 2_000
# MiniCode keeps a 50,000-character generic model-visible boundary while
# byte-oriented adapters (including delegated-result transport) use 50 KiB.
# They must remain distinct because 50,000 CJK characters are about 150 KiB.
MAX_TOOL_RESULT_CHARS = 50_000
MAX_TOOL_RESULT_BYTES = 50 * 1024  # 50 KiB transport/persistence cap
WORKSPACE_PATH_SCHEMA_FIELDS = frozenset({
    "file_path",
    "directory",
    "path",
    "cwd",
    "workspace_root",
    "working_directory",
    "dest",
    "root",
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


@dataclass(frozen=True)
class TextTruncationResult:
    """Python port of MiniCode's ``truncateHead`` result contract."""

    content: str
    truncated: bool
    truncated_by: str | None
    total_lines: int
    total_bytes: int
    output_lines: int
    output_bytes: int
    first_line_exceeds_limit: bool
    max_lines: int
    max_bytes: int


@dataclass(frozen=True)
class TailTextTruncationResult:
    """Python port of MiniCode's ``truncateTail`` result contract."""

    content: str
    truncated: bool
    truncated_by: str | None
    total_lines: int
    total_bytes: int
    output_lines: int
    output_bytes: int
    last_line_partial: bool
    max_lines: int
    max_bytes: int


def _split_lines_for_counting(content: str) -> list[str]:
    if not content:
        return []
    lines = content.split("\n")
    if content.endswith("\n"):
        lines.pop()
    return lines


def truncate_text_head(
    content: str,
    *,
    max_lines: int = MAX_TOOL_RESULT_LINES,
    max_bytes: int = MAX_TOOL_RESULT_BYTES,
) -> TextTruncationResult:
    """Keep complete leading lines until MiniCode's line or UTF-8 byte limit wins."""

    max_lines = max(1, int(max_lines))
    max_bytes = max(1, int(max_bytes))
    encoded_bytes = len(content.encode("utf-8"))
    lines = _split_lines_for_counting(content)
    total_lines = len(lines)
    if total_lines <= max_lines and encoded_bytes <= max_bytes:
        return TextTruncationResult(
            content=content,
            truncated=False,
            truncated_by=None,
            total_lines=total_lines,
            total_bytes=encoded_bytes,
            output_lines=total_lines,
            output_bytes=encoded_bytes,
            first_line_exceeds_limit=False,
            max_lines=max_lines,
            max_bytes=max_bytes,
        )

    first_line_bytes = len(lines[0].encode("utf-8")) if lines else 0
    if first_line_bytes > max_bytes:
        return TextTruncationResult(
            content="",
            truncated=True,
            truncated_by="bytes",
            total_lines=total_lines,
            total_bytes=encoded_bytes,
            output_lines=0,
            output_bytes=0,
            first_line_exceeds_limit=True,
            max_lines=max_lines,
            max_bytes=max_bytes,
        )

    output: list[str] = []
    output_bytes = 0
    truncated_by = "lines"
    for index, line in enumerate(lines[:max_lines]):
        line_bytes = len(line.encode("utf-8")) + (1 if index > 0 else 0)
        if output_bytes + line_bytes > max_bytes:
            truncated_by = "bytes"
            break
        output.append(line)
        output_bytes += line_bytes
    if len(output) >= max_lines and output_bytes <= max_bytes:
        truncated_by = "lines"
    rendered = "\n".join(output)
    return TextTruncationResult(
        content=rendered,
        truncated=True,
        truncated_by=truncated_by,
        total_lines=total_lines,
        total_bytes=encoded_bytes,
        output_lines=len(output),
        output_bytes=len(rendered.encode("utf-8")),
        first_line_exceeds_limit=False,
        max_lines=max_lines,
        max_bytes=max_bytes,
    )


def truncate_tool_result(content: str, max_chars: int = MAX_TOOL_RESULT_CHARS) -> str:
    """Apply MiniCode's complete-line character and line limits."""

    rendered, truncated_by, output_lines, total_lines, first_line_exceeds = (
        _truncate_text_head_by_chars(content, max_chars=max_chars)
    )
    if truncated_by is None:
        return content
    if first_line_exceeds:
        return f"[First line exceeds {max(1, int(max_chars))} character limit.]"
    if truncated_by == "lines":
        notice = (
            f"[Truncated: showing {output_lines} of {total_lines} lines "
            f"({MAX_TOOL_RESULT_LINES} line limit).]"
        )
    else:
        notice = (
            f"[Truncated: {output_lines} lines shown "
            f"({max(1, int(max_chars))} character limit).]"
        )
    return f"{rendered}\n\n{notice}" if rendered else notice


def tool_result_exceeds_inline_limit(
    content: str,
    max_chars: int = MAX_TOOL_RESULT_CHARS,
) -> bool:
    """Return whether MiniCode's generic model-visible result limit is exceeded."""

    return _truncate_text_head_by_chars(content, max_chars=max_chars)[1] is not None


def _truncate_text_head_by_chars(
    content: str,
    *,
    max_chars: int,
) -> tuple[str, str | None, int, int, bool]:
    max_chars = max(1, int(max_chars))
    lines = _split_lines_for_counting(content)
    total_lines = len(lines)
    if total_lines <= MAX_TOOL_RESULT_LINES and len(content) <= max_chars:
        return content, None, total_lines, total_lines, False
    if lines and len(lines[0]) > max_chars:
        return "", "chars", 0, total_lines, True

    output: list[str] = []
    output_chars = 0
    truncated_by = "lines"
    for index, line in enumerate(lines[:MAX_TOOL_RESULT_LINES]):
        line_chars = len(line) + (1 if index > 0 else 0)
        if output_chars + line_chars > max_chars:
            truncated_by = "chars"
            break
        output.append(line)
        output_chars += line_chars
    return "\n".join(output), truncated_by, len(output), total_lines, False


def _truncate_utf8_from_end(content: str, max_bytes: int) -> str:
    encoded = content.encode("utf-8")
    if len(encoded) <= max_bytes:
        return content
    start = len(encoded) - max_bytes
    while start < len(encoded) and encoded[start] & 0xC0 == 0x80:
        start += 1
    return encoded[start:].decode("utf-8")


def truncate_text_tail(
    content: str,
    *,
    max_lines: int = MAX_TOOL_RESULT_LINES,
    max_bytes: int = MAX_TOOL_RESULT_BYTES,
) -> TailTextTruncationResult:
    """Keep the final complete lines under MiniCode's 2000-line/50-KiB contract."""

    max_lines = max(1, int(max_lines))
    max_bytes = max(1, int(max_bytes))
    total_bytes = len(content.encode("utf-8"))
    lines = _split_lines_for_counting(content)
    total_lines = len(lines)
    if total_lines <= max_lines and total_bytes <= max_bytes:
        return TailTextTruncationResult(
            content=content,
            truncated=False,
            truncated_by=None,
            total_lines=total_lines,
            total_bytes=total_bytes,
            output_lines=total_lines,
            output_bytes=total_bytes,
            last_line_partial=False,
            max_lines=max_lines,
            max_bytes=max_bytes,
        )

    output: list[str] = []
    output_bytes = 0
    truncated_by = "lines"
    last_line_partial = False
    for line in reversed(lines):
        if len(output) >= max_lines:
            break
        line_bytes = len(line.encode("utf-8")) + (1 if output else 0)
        if output_bytes + line_bytes > max_bytes:
            truncated_by = "bytes"
            if not output:
                partial = _truncate_utf8_from_end(line, max_bytes)
                output.insert(0, partial)
                output_bytes = len(partial.encode("utf-8"))
                last_line_partial = True
            break
        output.insert(0, line)
        output_bytes += line_bytes

    if len(output) >= max_lines and output_bytes <= max_bytes:
        truncated_by = "lines"
    rendered = "\n".join(output)
    return TailTextTruncationResult(
        content=rendered,
        truncated=True,
        truncated_by=truncated_by,
        total_lines=total_lines,
        total_bytes=total_bytes,
        output_lines=len(output),
        output_bytes=len(rendered.encode("utf-8")),
        last_line_partial=last_line_partial,
        max_lines=max_lines,
        max_bytes=max_bytes,
    )


@dataclass
class ToolSchema:
    """JSON schema definition for an agent tool."""

    name: str
    description: str
    parameters: dict[str, Any]
    strict: bool = False

    @staticmethod
    def _supports_openai_strict(schema: Any) -> bool:
        """Return whether a schema can truthfully advertise OpenAI strict mode.

        OpenAI strict tools require every property of every object schema to
        be listed in ``required``. MiniCode leaves tools with optional parameters
        at ``strict: false``; silently making those parameters required would
        change the tool contract.
        """
        if isinstance(schema, list):
            return all(ToolSchema._supports_openai_strict(item) for item in schema)
        if not isinstance(schema, dict):
            return True
        properties = schema.get("properties")
        if isinstance(properties, dict):
            required = schema.get("required")
            if not isinstance(required, list) or set(required) != set(properties):
                return False
            if not all(
                ToolSchema._supports_openai_strict(value)
                for value in properties.values()
            ):
                return False
        if "items" in schema and not ToolSchema._supports_openai_strict(schema["items"]):
            return False
        for key in ("anyOf", "oneOf", "allOf"):
            if key in schema and not ToolSchema._supports_openai_strict(schema[key]):
                return False
        for key in ("$defs", "definitions"):
            definitions = schema.get(key)
            if isinstance(definitions, dict) and not all(
                ToolSchema._supports_openai_strict(value)
                for value in definitions.values()
            ):
                return False
        return True

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
        if self.strict and self._supports_openai_strict(self.parameters):
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
    # Execution metadata. Tools self-declare these instead of the runtime
    # keeping hardcoded per-name tables. None means the call is bounded only by
    # the enclosing turn deadline, when one is configured.
    timeout_seconds: float | None = None
    mutates_workspace: bool = False  # writes files / workspace state
    mutates_external_state: bool = False  # commits, sends, external side effects
    # Side-effect/idempotency policy. ``None`` means derive from legacy
    # capability hints; subclasses can set these to make retry/prefetch
    # decisions explicit. ``side_effect_kind`` is intentionally coarse because
    # it is used for runtime scheduling and UI diagnostics, not model prompting.
    side_effect_kind: str | None = None
    idempotent: bool | None = None
    streams_output: bool = False
    # Filesystem permission metadata. Tools declare which invocation fields
    # contain workspace paths; the permission layer applies one boundary policy
    # without classifying tools by name.
    workspace_path_fields: tuple[str, ...] = ()
    allow_workspace_root_path: bool = False
    allow_tool_result_path: bool = False
    # UI/event projection hints. These are non-model-facing and let each tool own
    # its display category. Undeclared tools remain generic.
    result_kind: str | None = None
    activity_kind: str | None = None
    display_label: str | None = None
    projection_visibility: str = "timeline"
    # Max characters of result content kept inline before the global truncation
    # backstop fires. Tools that already self-bound and store overflow as an
    # artifact (read_file, web_fetch, run_command) set this to None to skip the
    # backstop — double-truncating their compact summary loses head/tail context.
    max_result_chars: int | None = MAX_TOOL_RESULT_CHARS

    def get_workspace_paths(self, args: dict[str, Any] | None = None) -> list[str]:
        """Return invocation paths for the filesystem permission boundary.

        This is the same tool-owned path extraction seam used by MiniCode's
        filesystem permission checker. Most tools only need declarative fields;
        multi-file tools can override it when paths are embedded in a payload.
        """
        values: list[str] = []
        payload = args or {}
        fields = set(self.workspace_path_fields)
        try:
            schema = self.get_schema().parameters
            properties = schema.get("properties") if isinstance(schema, dict) else None
        except Exception:
            properties = None
        if isinstance(properties, dict):
            fields.update(WORKSPACE_PATH_SCHEMA_FIELDS.intersection(properties))
        for field in fields:
            value = payload.get(field)
            if isinstance(value, str) and value.strip() not in {"", "."}:
                values.append(value)
            elif isinstance(value, (list, tuple)):
                values.extend(
                    item
                    for item in value
                    if isinstance(item, str) and item.strip() not in {"", "."}
                )
        return values

    def model_description(self) -> str:
        """Compact capability description shown to the model in the tool schema.

        Defaults to ``description``. Kept separate from runtime/UI text so that
        permission, diff, and evidence metadata never leak into the function
        schema the model sees.
        """
        return getattr(self, "description", "") or ""

    def model_schema(self) -> ToolSchema:
        """Compact schema shown only to the model.

        Override when the runtime/UI schema should stay rich but the model-facing
        schema can be narrower or terser for latency and prompt-cache efficiency.
        """
        return self.get_execution_schema()

    def runtime_description(self) -> str:
        """Human/UI-facing description. Defaults to ``description``."""
        return getattr(self, "description", "") or ""

    def coerce_input(self, args: dict[str, Any]) -> dict[str, Any]:
        """Return the canonical arguments that should be validated/executed.

        Most tools must preserve their input byte-for-byte and therefore use
        this no-op implementation.  A protocol adapter may override the hook
        when the public runtime explicitly normalizes an otherwise valid call
        *before* JSON-schema validation (for example MiniCode truncates a
        long optional SendMessage summary instead of rejecting the call).

        The hook must be deterministic and idempotent.  ``validate_tool_input``
        writes the returned mapping back into the original argument object so
        permission checks, hooks, durable journaling and execution all observe
        one canonical payload even when validation is performed more than once.
        """
        return args

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
            "streams_output": self.streams_output,
            "workspace_path_fields": self.workspace_path_fields,
            "supports_idempotency_key": self.is_idempotent(),
            "max_result_chars": self.max_result_chars,
        }
        metadata.update(self.to_projection_metadata())
        return metadata

    def to_projection_metadata(self) -> dict[str, Any]:
        """Return UI projection hints owned by this tool."""
        metadata = {
            "result_kind": self.result_kind,
            "activity_kind": self.activity_kind,
            "display_label": self.display_label,
            "visibility": self.projection_visibility,
        }
        return {key: value for key, value in metadata.items() if value}

    def validate_input(self, args: dict[str, Any] | None = None) -> str:
        """Tool-owned semantic validation, run after schema validation.

        Return an empty string when input is acceptable, or a short actionable
        error to block permission/execution (CC's validateInput). The message is surfaced
        as an observation-style tool result so the model can correct the call.
        Defaults to no validation.
        """
        return ""

    def streamed_input_preview(
        self,
        args: dict[str, Any],
        context: Any | None = None,
        prior: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return safe, displayable fields from partially parsed arguments.

        Tools opt in explicitly, matching MiniCode's optional argument-diff
        consumer. Large bodies and raw partial JSON stay provider-internal.

        The returned dict may carry two projection-owned channels: a ``diff``
        key with live ``{"plus": n, "minus": m}`` line counts (the projection
        lifts it into the tool_call event instead of the public args), and
        underscore-prefixed keys that are private state handed back as
        ``prior`` on the next delta (e.g. a cached pre-edit line count) so a
        preview never re-reads the workspace per delta.
        """
        return {}

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

    def is_capability_available(
        self,
        context: "PermissionContext | None" = None,
    ) -> bool:
        """Return whether this tool belongs on the model capability surface.

        This is deliberately separate from ``check_permission``. The latter
        evaluates one concrete invocation and therefore cannot answer a schema
        question for path-dependent tools before their arguments exist.
        """
        del context
        return self.permission != PermissionLevel.ALWAYS_DENY

    def capability_permission_level(
        self,
        context: "PermissionContext | None" = None,
    ) -> PermissionLevel:
        """Return the capability's baseline UI permission without fake args."""
        del context
        return self.permission

    def is_concurrency_safe(self, args: dict[str, Any] | None = None) -> bool:
        """Return whether this tool can safely run alongside other read-only work."""
        # ``get_side_effect_kind`` is the canonical per-invocation policy.
        # Its default derives from the class-level mutation flags, while
        # argument-sensitive tools (notably run_command) can
        # safely classify a concrete read-only invocation as side-effect free.
        if self.get_side_effect_kind(args) != TOOL_SIDE_EFFECT_NONE:
            return False
        try:
            if self.is_read_only(args):
                return True
        except Exception:
            if self.read_only:
                return True
        return False

    def get_side_effect_kind(self, args: dict[str, Any] | None = None) -> str:
        """Return the coarse side-effect class for this invocation.

        Tools with argument-dependent behavior can override this method. The
        default derives from the tool's declared capability hints.
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
        # Idempotency keys are used for durable deduplication; keep the full
        # digest so distinct calls cannot collide behind a display shortcut.
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return f"{self.name}:{digest}"

    @abstractmethod
    def get_schema(self) -> ToolSchema:
        """Return the JSON schema for this tool."""

    def get_execution_schema(self) -> ToolSchema:
        """Return the host-side schema accepted at the execution boundary.

        Most tools expose and execute the same shape.  A control tool may have
        additional host-owned fields supplied by an approval response (for
        example an edited plan) that must be validated without advertising
        those fields to the model.  Such tools override ``model_schema``;
        this method owns every runtime-executable field, including private
        harness-injected guards.
        """

        return self.get_schema()

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
        detail = str(message or "Tool execution failed.").strip()
        return ToolResult(
            content=f"Error: {detail}",
            is_error=True,
            error_kind="execution_error",
            user_summary="工具执行失败。",
            developer_detail=detail,
            recoverable=True,
            projection="error",
            model_observation=(
                f"The {getattr(self, 'name', 'tool')} call failed. "
                "Use the error detail to correct the call or try another approach."
            ),
        )

    def _success_result(
        self,
        content: str,
        artifact_id: str | None = None,
        artifact_preview: str | None = None,
        display_summary: str | None = None,
    ) -> ToolResult:
        """Return a standardized success result."""
        return ToolResult(
            content=content,
            artifact_id=artifact_id,
            artifact_preview=artifact_preview,
            display_summary=display_summary,
        )


def validate_tool_input(tool: BaseTool, args: Any) -> str:
    """Coerce protocol input, validate schema, then tool semantics."""
    if not isinstance(args, dict):
        return "arguments: expected an object"

    try:
        coerced = tool.coerce_input(args)
    except Exception as exc:
        return f"Tool input coercion failed ({type(exc).__name__})."
    if not isinstance(coerced, dict):
        return "Tool input coercion must return an object"
    if coerced is not args:
        # Preserve object identity: upstream permission and hook owners retain
        # references to this exact mapping.  Clear/update also removes fields
        # intentionally dropped by a protocol normalizer.
        args.clear()
        args.update(coerced)

    try:
        schema = tool.get_execution_schema().parameters
        validator_class = validator_for(schema)
        validator_class.check_schema(schema)
        errors = sorted(
            validator_class(schema).iter_errors(args),
            key=lambda error: tuple(str(part) for part in error.absolute_path),
        )
    except SchemaError as exc:
        return f"Tool schema is invalid: {exc.message}"
    except Exception as exc:
        return f"Tool schema validation failed ({type(exc).__name__})."

    if errors:
        error = errors[0]
        path = ".".join(str(part) for part in error.absolute_path)
        return f"{path or 'arguments'}: {error.message}"

    try:
        return str(tool.validate_input(args) or "").strip()
    except Exception as exc:
        return f"Tool input validation failed ({type(exc).__name__})."
