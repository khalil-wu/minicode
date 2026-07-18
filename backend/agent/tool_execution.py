from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

from backend.agent.context import ContextBuilder
from backend.agent.coordinator import (
    coordinator_delegation_block_reason,
    coordinator_mode_enabled,
    coordinator_tool_block_reason,
)
from backend.tools.contracts import EvidenceRecord, ToolOutcome, ToolOutcomeStatus
from backend.agent.message import AgentEvent
from backend.agent.runtime_spans import runtime_span_from_tool_context
from backend.agent.tool_common import WEB_SEARCH_TOOL_NAMES, WEB_FETCH_TOOL_NAMES, WEB_TOOL_NAMES, _text_arg
from backend.agent.tool_guardrails import (
    ToolCallSignature,
    ToolCallGuardrailController,
    _search_query_similarity,
)
from backend.agent.tool_execution_guardrails import (
    duplicate_output_write_guard_result,
    guardrail_after_call_result,
    guardrail_before_call_result,
    invalid_call_result as _invalid_call_result,
    rejection_result as _rejection_result,
)
from backend.agent.state import AgentState
from backend.agent.tool_events import (
    describe_tool_call,
    panel_hint_for_tool_result as _panel_hint_for_tool_result,
    requires_attention_for_tool_result as _requires_attention_for_tool_result,
    status_for_result,
    tool_call_start_event,
    tool_start_times as _tool_start_times,
)
from backend.agent.tool_issues import classify_tool_issue
from backend.agent.tool_repair import RepairResult, ToolArgRepairEngine, argument_has_value
from backend.agent.tool_resources import (
    ResourceResolver,
    clean_candidate_url,
    inferred_read_file_path_from_recent_list,
)
from backend.tools.catalog import tool_spec_for
from backend.tools.tool_search import deferred_catalog_scope_allows
from backend.agent.control_tools import CONTROL_TOOL_NAMES, ControlToolRouter
from backend.agent.tool_projection import (
    DEFAULT_PROJECTION_REGISTRY,
    display_summary_for_result,
    result_kind_for_tool,
)
from backend.agent.tool_runtime import (
    CHECKPOINT_WRITE_TOOL_NAMES,
    TOOL_TIMEOUTS,
    resolve_max_concurrent_tools as _resolve_max_concurrent_tools,
    resolve_tool_batch_timeout as _resolve_tool_batch_timeout,
    resolve_tool_timeout as _resolve_tool_timeout,
    tool_batch_timeout_result as _tool_batch_timeout_result,
    tool_is_idempotent as _tool_is_idempotent,
    tool_mutates as _tool_mutates,
    tool_side_effect_kind as _tool_side_effect_kind,
)
from backend.llm.base import ToolCallEvent
from backend.permissions.checker import (
    PermissionChecker,
    evaluate_permission_decision,
)
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.permissions.review import generate_edit_diff_payload, generate_file_diff_payload
from backend.tools.base import PermissionLevel, ToolResult
from backend.tools.registry import ToolRegistry
from backend.tools.subagent_context import is_subagent_permission_context, subagent_toolset_policy

logger = logging.getLogger(__name__)

SPECIAL_TOOL_NAMES = CONTROL_TOOL_NAMES
INTERNAL_GUARDED_TOOL_NAMES = {
    "web_search",
    "web_fetch",
    "run_command",
}
NON_CRITICAL_TIMEOUT_TOOLS = {
    "task",
    "save_memory",
}
COMMAND_OUTPUT_STREAM_TOOL_NAMES = {"run_command", "bash", "powershell"}
CLAUDE_CODE_TOOL_NAME_ALIASES = {
    "Read": "read_file",
}
WORKSPACE_FILE_ARG_ALIAS_TO_CANONICAL = {
    "read_file": ("file_path", ("path", "target", "filename")),
    "write_file": ("file_path", ("path", "target", "filename")),
    "edit_file": ("file_path", ("path", "target", "filename")),
}
WORKSPACE_WRITE_TOOL_NAMES = {"write_file", "edit_file", "apply_patch"}
FORCE_TOOL_RESULT_ARTIFACT_CHARS = 16_000
FORCE_TOOL_RESULT_ARTIFACT_PREVIEW_LINES = 8
SKIP_FORCED_ARTIFACT_TOOL_NAMES = {"read_artifact"}


@dataclass(frozen=True)
class _ToolBatchRuntime:
    ctx: ContextBuilder
    state: AgentState
    tool_registry: ToolRegistry
    tool_ctx: ToolExecutionContext
    iteration_id: str
    turn_id: str = ""
    guardrail_controller: ToolCallGuardrailController | None = None


@dataclass
class PrefetchedToolExecution:
    """Background execution started when a complete safe tool block arrives."""

    tool_call: ToolCallEvent
    task: asyncio.Task[ToolResult]
    started_epoch: float


_SHELL_FILE_WRITE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Any stdout-style redirect (bare or numbered fd). Bit-buckets are masked
    # first, so `> /dev/null` / `2> nul` never reach these patterns.
    re.compile(r"(?<![0-9])>{1,2}\s*(?!&)\S+", re.I),
    re.compile(r"\b\d+>{1,2}\s*(?!&)\S+", re.I),
    re.compile(r"&>{1,2}\s*\S+", re.I),
    re.compile(r"\b(?:set-content|add-content|out-file|tee-object)\b", re.I),
    re.compile(r"\btee\b", re.I),
    re.compile(r"\bcopy\s+(?:nul|/y\b|con\b).+\S", re.I),
    re.compile(r"\btype\s+nul\s*>\s*\S+", re.I),
    re.compile(r"\bcat\s+>{1,2}\s*\S+", re.I),
)


# Language payloads live inside quotes; match them against the original command
# so quote-masking does not hide real write APIs.
_SHELL_INLINE_WRITE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bpython(?:\d+(?:\.\d+)?)?\s+-c\b.*\bopen\s*\([^)]*['\"][wa]\b", re.I | re.S),
    re.compile(r"\bpython(?:\d+(?:\.\d+)?)?\s+-c\b.*\bwrite_text\s*\(", re.I | re.S),
    re.compile(r"\bnode\s+-e\b.*\b(?:writeFileSync|appendFileSync)\s*\(", re.I | re.S),
    re.compile(r"\b(?:powershell|pwsh)\b.*-(?:Command|EncodedCommand)\b.*\b(?:set-content|add-content|out-file|tee-object)\b", re.I | re.S),
)

# Quoted spans (so '>' inside a JS/Python string literal or a grep/git -S pattern
# is not mistaken for a shell redirection) and bit-bucket redirections (so
# `> /dev/null` / `> nul` discards are not mistaken for file writes).
_QUOTED_SPAN_RE = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'')
_BITBUCKET_RE = re.compile(r"\d*>+\s*(?:/dev/null|nul)\b", re.I)


def _mask_command_for_write_guard(command: str) -> str:
    # Mask every quoted span for redirect detection. Real write APIs inside
    # python/node/powershell payloads are checked separately on the original.
    masked = _QUOTED_SPAN_RE.sub('""', command)
    return _BITBUCKET_RE.sub(" ", masked)


def _tool_turn_id(tool_ctx: ToolExecutionContext) -> str:
    metadata = tool_ctx.metadata if isinstance(tool_ctx.metadata, dict) else {}
    return str(metadata.get("turn_id") or metadata.get("assistant_message_id") or "").strip()


def _artifact_store_from_tool_context(tool_ctx: ToolExecutionContext | None) -> Any | None:
    if tool_ctx is None:
        return None
    artifact_store = getattr(tool_ctx, "artifact_store", None)
    if artifact_store is not None:
        return artifact_store
    metadata = tool_ctx.metadata if isinstance(tool_ctx.metadata, dict) else {}
    return metadata.get("artifact_store")


def _force_artifact_for_oversized_tool_result(
    tc: ToolCallEvent,
    result: ToolResult,
    tool_ctx: ToolExecutionContext | None,
) -> ToolResult:
    """Persist very large raw tool output and keep only stable metadata inline."""
    content = result.content or ""
    if (
        result.artifact_id
        or tc.name in SKIP_FORCED_ARTIFACT_TOOL_NAMES
        or len(content) <= FORCE_TOOL_RESULT_ARTIFACT_CHARS
    ):
        return result

    artifact_store = _artifact_store_from_tool_context(tool_ctx)
    if artifact_store is None or not hasattr(artifact_store, "save"):
        return result

    content_hash = hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()
    line_count = len(content.splitlines())
    try:
        artifact_id = artifact_store.save(
            content=content,
            source=f"{tc.name}({tc.id})",
            type="tool_result",
            preview_lines=FORCE_TOOL_RESULT_ARTIFACT_PREVIEW_LINES,
        )
    except Exception as exc:
        logger.warning(
            "large tool result artifact save failed tool=%s call_id=%s chars=%s error=%r",
            tc.name,
            tc.id,
            len(content),
            exc,
        )
        return result

    summary = "\n".join(
        [
            "Large tool result stored as artifact.",
            f"tool: {tc.name}",
            f"artifact_id: {artifact_id}",
            f"original_chars: {len(content)}",
            f"line_count: {line_count}",
            f"content_hash: sha256:{content_hash[:16]}",
        ]
    )
    return replace(
        result,
        content=summary,
        artifact_id=artifact_id,
        display_summary=result.display_summary or f"Stored large result from {tc.name} as artifact",
        limitation=result.limitation or "large result stored as artifact",
    )


def run_command_file_write_guard_reason(command: str) -> str:
    """Return guidance when run_command is being used as a file editing tool."""
    stripped = command.strip()
    if not stripped:
        return ""
    for pattern in _SHELL_INLINE_WRITE_PATTERNS:
        if pattern.search(stripped):
            return (
                "Blocked run_command because it appears to create or edit files through the shell. "
                "Use write_file for complete file writes or edit_file for targeted changes so MiniCode can show a diff review. "
                "Use run_command only for commands such as tests, builds, git inspection, and other shell-only operations."
            )
    masked = _mask_command_for_write_guard(stripped)
    for pattern in _SHELL_FILE_WRITE_PATTERNS:
        if pattern.search(masked):
            return (
                "Blocked run_command because it appears to create or edit files through the shell. "
                "Use write_file for complete file writes or edit_file for targeted changes so MiniCode can show a diff review. "
                "Use run_command only for commands such as tests, builds, git inspection, and other shell-only operations."
            )
    return ""


def _metadata_bool(metadata: dict[str, Any], key: str) -> bool:
    value = metadata.get(key)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _metadata_string_list(metadata: dict[str, Any], key: str) -> list[str]:
    value = metadata.get(key)
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _tool_call_is_read_only(tc: ToolCallEvent, tool_registry: ToolRegistry) -> bool:
    tool = tool_registry.get_tool(tc.name)
    if tool is not None:
        try:
            return bool(tool.is_read_only(tc.arguments))
        except Exception:
            return bool(getattr(tool, "read_only", False))
    return not _tool_mutates(tc.name, tool_registry)


def _resolve_workspace_path_for_scope(raw_path: str, tool_ctx: ToolExecutionContext) -> Path:
    from backend.tools.path_resolution import _resolve_path

    return _resolve_path(raw_path, tool_ctx, allow_workspace_escape=False).resolve()


def _apply_patch_target_paths(patch_text: str) -> list[str] | None:
    try:
        from backend.tools.apply_patch_parser import patch_target_paths

        return patch_target_paths(patch_text)
    except Exception:
        return None


def _workspace_write_targets(tc: ToolCallEvent) -> list[str] | None:
    if tc.name in {"write_file", "edit_file"}:
        raw = tc.arguments.get("file_path")
        text = str(raw or "").strip()
        return [text] if text else []
    if tc.name == "apply_patch":
        patch = tc.arguments.get("patch")
        if not isinstance(patch, str):
            return []
        return _apply_patch_target_paths(patch)
    return None


def _path_is_within_any_scope(path: Path, scopes: list[Path]) -> bool:
    for scope in scopes:
        if path == scope:
            return True
        try:
            path.relative_to(scope)
            return True
        except ValueError:
            continue
    return False


def subagent_scope_guard_reason(
    tc: ToolCallEvent,
    tool_registry: ToolRegistry,
    tool_ctx: ToolExecutionContext,
) -> str:
    metadata = tool_ctx.metadata if isinstance(tool_ctx.metadata, dict) else {}
    read_only = _metadata_bool(metadata, "read_only")
    write_scope = _metadata_string_list(metadata, "write_scope")
    if not read_only and not write_scope:
        return ""

    if read_only and not _tool_call_is_read_only(tc, tool_registry):
        return (
            f"Blocked tool '{tc.name}' because this subagent is marked read_only. "
            "Return findings without modifying files, running mutating commands, or changing external state."
        )

    if not write_scope or tc.name not in WORKSPACE_WRITE_TOOL_NAMES:
        return ""

    raw_targets = _workspace_write_targets(tc)
    if raw_targets is None:
        return ""
    if not raw_targets:
        return (
            f"Blocked tool '{tc.name}' because this subagent has write_scope={write_scope} "
            "but the target path could not be determined."
        )

    try:
        resolved_scopes = [
            _resolve_workspace_path_for_scope(scope, tool_ctx)
            for scope in write_scope
        ]
        resolved_targets = [
            _resolve_workspace_path_for_scope(target, tool_ctx)
            for target in raw_targets
        ]
    except Exception as exc:
        return f"Blocked tool '{tc.name}' because write_scope path validation failed: {exc}"

    outside = [
        str(target)
        for target in resolved_targets
        if not _path_is_within_any_scope(target, resolved_scopes)
    ]
    if outside:
        return (
            f"Blocked tool '{tc.name}' because it writes outside this subagent's write_scope. "
            f"Allowed scope(s): {', '.join(write_scope)}. Outside target(s): {', '.join(outside)}."
        )
    return ""


def is_malformed_web_tool_call(reason: str) -> bool:
    return bool(
        re.search(
            r"(?:Invalid web_(?:search|fetch) call|Invalid tool call for 'web_(?:search|fetch)'): "
            r"missing required",
            reason,
            re.I,
        )
    )


def disabled_tool_guard_reason(state: AgentState, tc: ToolCallEvent) -> str:
    if tc.name not in state.disabled_tools:
        return ""
    guidance = " ".join(state.loop_guidance[-2:]).strip()
    if guidance:
        return f"Tool '{tc.name}' is disabled for this turn. {guidance}"
    return f"Tool '{tc.name}' is disabled for this turn. Continue without calling it."


def repeated_similar_web_search_result(state: AgentState, tc: ToolCallEvent) -> ToolResult | None:
    if tc.name not in WEB_SEARCH_TOOL_NAMES:
        return None
    query = _text_arg((tc.arguments or {}).get("query"))
    if not query:
        return None
    similar_records = [
        record
        for record in state.tool_calls
        if record.tool_name in WEB_SEARCH_TOOL_NAMES
        and _search_query_similarity(query, _text_arg(record.tool_input.get("query"))) >= 0.67
    ]
    if len(similar_records) < 2:
        return None

    guidance = (
        "相似网页搜索未返回结果或足够新结果。请停止继续调用 web_search，"
        "基于已有候选来源回答；如果证据不足，请明确说明限制。"
    )
    state.disable_tools({"web_search"}, guidance)
    return ToolResult(
        content=guidance,
        is_error=False,
        status="success",
        display_summary=guidance,
        result_kind="search",
        evidence_type="candidate",
    )


def normalize_tool_arguments(name: str, args: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(args)
    if name in {"load_skill", "unload_skill"} and not argument_has_value(normalized, "skill_name"):
        for alias in ("skillName", "skill", "name"):
            if argument_has_value(normalized, alias):
                normalized["skill_name"] = normalized[alias]
                break
    canonical_path = WORKSPACE_FILE_ARG_ALIAS_TO_CANONICAL.get(name)
    if canonical_path is not None:
        canonical, aliases = canonical_path
        if not argument_has_value(normalized, canonical):
            for alias in aliases:
                if argument_has_value(normalized, alias):
                    normalized[canonical] = normalized[alias]
                    break
    if name in WEB_SEARCH_TOOL_NAMES and not _text_arg(normalized.get("query")):
        query = (
            _text_arg(normalized.get("q"))
            or _text_arg(normalized.get("search_query"))
            or _text_arg(normalized.get("queries"))
            or _text_arg(normalized.get("pattern"))
        )
        if query:
            normalized["query"] = query
    if name in WEB_FETCH_TOOL_NAMES and not _text_arg(normalized.get("url")):
        url = _text_arg(normalized.get("href")) or _text_arg(normalized.get("link"))
        if url:
            normalized["url"] = url
    return normalized


def normalize_tool_call_event(tc: ToolCallEvent, *, fallback_id: str = "") -> ToolCallEvent:
    raw_name = str(tc.name or "").strip()
    name = CLAUDE_CODE_TOOL_NAME_ALIASES.get(raw_name, raw_name)
    args = tc.arguments if isinstance(tc.arguments, dict) else {}
    return replace(
        tc,
        id=str(tc.id or "").strip() or fallback_id,
        name=name,
        arguments=normalize_tool_arguments(name, args),
    )


def unwrap_deferred_tool_call(
    tc: ToolCallEvent,
    tool_registry: ToolRegistry,
    permission_context: PermissionContext | None = None,
) -> tuple[ToolCallEvent, str]:
    if tc.name != "tool_call":
        return tc, ""
    underlying_name = str((tc.arguments or {}).get("name") or "").strip()
    underlying_args = (tc.arguments or {}).get("arguments")
    if not underlying_name:
        return tc, "tool_call is missing required field 'name'."
    if not isinstance(underlying_args, dict):
        return tc, "tool_call.arguments must be an object."
    if tool_registry.get_tool(underlying_name) is None:
        return tc, f"Deferred tool '{underlying_name}' does not exist."
    toolset_policy = subagent_toolset_policy() if is_subagent_permission_context(permission_context) else None
    get_view = getattr(tool_registry, "get_schema_view", None)
    view = (
        get_view(underlying_name, toolset_policy=toolset_policy)
        if callable(get_view)
        else None
    )
    if view is not None:
        if view.exposure != "deferred" or view.direct:
            return tc, f"Tool '{underlying_name}' is not available as a deferred tool."
        if not deferred_catalog_scope_allows(view.runtime_metadata or {}, "default"):
            return tc, f"Tool '{underlying_name}' is not available as a deferred tool."
        return replace(tc, name=underlying_name, arguments=underlying_args), ""
    spec = tool_spec_for(underlying_name, tool_registry)
    if spec.exposure != "deferred" or getattr(spec, "always_load", False):
        return tc, f"Tool '{underlying_name}' is not available as a deferred tool."
    tool = tool_registry.get_tool(underlying_name)
    meta = tool.to_runtime_metadata() if tool is not None and hasattr(tool, "to_runtime_metadata") else {}
    if not deferred_catalog_scope_allows(meta, "default"):
        return tc, f"Tool '{underlying_name}' is not available as a deferred tool."
    return replace(tc, name=underlying_name, arguments=underlying_args), ""


def normalized_tool_arguments(name: str, args: dict[str, Any] | None) -> dict[str, Any]:
    return normalize_tool_arguments(name, dict(args or {}))


def missing_required_tool_argument_names(tc: ToolCallEvent, tool_registry: ToolRegistry) -> list[str]:
    # Compute on a local copy; do NOT mutate the caller's tc. This is a query
    # helper called from history-safety / prefetch paths on tc objects that are
    # also written to history. Reassigning tc.arguments here was a hidden source
    # of prefetch-signature drift (args rewritten under the prefetch's feet).
    args = normalized_tool_arguments(tc.name, tc.arguments)
    tool = tool_registry.get_tool(tc.name)
    if tool is None:
        return []
    try:
        schema = tool.get_schema()
    except Exception:
        return []
    required_fields = schema.parameters.get("required", []) if schema else []
    return [
        str(field)
        for field in required_fields
        if isinstance(field, str) and not argument_has_value(args, field)
    ]


def missing_required_tool_argument_reason(
    state: AgentState,
    tc: ToolCallEvent,
    tool_registry: ToolRegistry,
) -> str:
    return ToolArgRepairEngine(state, tool_registry).missing_required_reason(tc)


def tool_call_needs_list_context(tc: ToolCallEvent, tool_registry: ToolRegistry) -> bool:
    spec = tool_spec_for(tc.name, tool_registry)
    for arg in spec.required_args:
        if spec.role_for(arg) == "workspace_file" and not argument_has_value(tc.arguments or {}, arg):
            return True
    return False


def _dedupe_tool_call_ids(tool_calls: list[ToolCallEvent]) -> list[ToolCallEvent]:
    """Force every tool_call id in a batch to be non-empty and unique.

    OpenAI-compatible providers require each assistant ``tool_calls`` entry to
    have exactly one matching ``tool`` reply, keyed by id. Streamed providers
    (DeepSeek-style) can emit blank or repeated ids across a batch; a collision
    makes two tool results share one id, so the next request is rejected for a
    missing/duplicate reply. We suffix repeats with ``:dup{n}`` so history and
    tool results stay in lockstep. Idempotent: an already-unique batch is
    returned unchanged (suffix only applies on a real collision), so running
    this again on the same batch is a no-op.
    """
    seen: set[str] = set()
    result: list[ToolCallEvent] = []
    for index, tc in enumerate(tool_calls, 1):
        call_id = str(tc.id or "").strip() or f"tool_{index}"
        if call_id in seen:
            suffix = 2
            while f"{call_id}:dup{suffix}" in seen:
                suffix += 1
            call_id = f"{call_id}:dup{suffix}"
        seen.add(call_id)
        result.append(tc if tc.id == call_id else replace(tc, id=call_id))
    return result


def prepare_tool_call_sequence(
    state: AgentState,
    tool_calls: list[ToolCallEvent],
    tool_registry: ToolRegistry,
    tool_ctx: ToolExecutionContext | None = None,
) -> list[RepairResult]:
    """Normalize and repair a model tool-call batch once for history and execution."""
    reserved_fetch_urls: set[str] = set()
    repaired: list[RepairResult] = []
    for index, raw_tc in enumerate(tool_calls, 1):
        tc = normalize_tool_call_event(raw_tc, fallback_id=f"tool_{index}")
        repair_result = repair_tool_call_for_execution(
            state,
            tc,
            tool_registry,
            tool_ctx,
            reserved_fetch_urls=reserved_fetch_urls,
        )
        tc = repair_result.tool_call
        if tc.name in WEB_FETCH_TOOL_NAMES:
            url = _text_arg((tc.arguments or {}).get("url"))
            if url:
                reserved_fetch_urls.add(clean_candidate_url(url))
        repaired.append(repair_result)
    # Final guard: ids must be unique + non-empty across the whole batch before
    # they reach history (append_assistant_tool_calls) or the parallel executor
    # (results_by_id keyed by id). Done last so per-call repair can't reintroduce
    # a collision.
    deduped = _dedupe_tool_call_ids([result.tool_call for result in repaired])
    return [
        result if result.tool_call == tool_call else replace(result, tool_call=tool_call)
        for result, tool_call in zip(repaired, deduped)
    ]


def repair_tool_call_sequence(
    state: AgentState,
    tool_calls: list[ToolCallEvent],
    tool_registry: ToolRegistry,
    tool_ctx: ToolExecutionContext | None = None,
) -> list[ToolCallEvent]:
    """Compatibility projection of the prepared batch to repaired tool calls."""
    return [
        result.tool_call
        for result in prepare_tool_call_sequence(state, tool_calls, tool_registry, tool_ctx)
    ]


def repair_tool_call_for_execution(
    state: AgentState,
    tc: ToolCallEvent,
    tool_registry: ToolRegistry,
    tool_ctx: ToolExecutionContext | None = None,
    *,
    reserved_fetch_urls: set[str] | None = None,
) -> RepairResult:
    """Run the structured argument-repair path used by tool execution."""
    resolver = ResourceResolver(state, tool_ctx, reserved_fetch_urls=reserved_fetch_urls)
    return ToolArgRepairEngine(state, tool_registry, resolver).repair_result(tc)


def repair_result_block_reason(
    state: AgentState,
    repair_result: RepairResult,
    tool_registry: ToolRegistry,
) -> str:
    """Project a structured repair failure into model-facing guidance."""
    if repair_result.needs_model_generation or repair_result.routing_correction:
        return ""
    if not (repair_result.needs_user_input or repair_result.blocked):
        return ""
    tc = repair_result.tool_call
    required_reason = missing_required_tool_argument_reason(state, tc, tool_registry)
    details = [
        text.strip()
        for text in (
            repair_result.user_message,
            repair_result.model_observation,
            required_reason,
        )
        if text and text.strip()
    ]
    return " ".join(dict.fromkeys(details))


def tool_call_is_safe_for_model_history(tc: ToolCallEvent, tool_registry: ToolRegistry) -> bool:
    normalized = normalize_tool_call_event(tc)
    if not normalized.id or not normalized.name:
        return False
    if invalid_tool_call_guard_reason(normalized, tool_registry):
        return False
    if ToolArgRepairEngine(AgentState(user_message=""), tool_registry).missing_required_reason(normalized):
        return False
    if missing_required_tool_argument_names(normalized, tool_registry):
        return False
    return True


def _matching_prefetch(
    prefetched_results: dict[str, PrefetchedToolExecution] | None,
    tc: ToolCallEvent,
) -> PrefetchedToolExecution | None:
    if not prefetched_results:
        return None
    prefetched = prefetched_results.get(tc.id)
    if prefetched is None:
        return None
    if (
        str(prefetched.tool_call.name or "") != str(tc.name or "")
        or prefetched.tool_call.arguments != tc.arguments
    ):
        return None
    return prefetched


def _take_matching_prefetch(
    prefetched_results: dict[str, PrefetchedToolExecution] | None,
    tc: ToolCallEvent,
) -> PrefetchedToolExecution | None:
    prefetched = _matching_prefetch(prefetched_results, tc)
    if prefetched is None or prefetched_results is None:
        return None
    return prefetched_results.pop(tc.id, None)


async def _await_prefetched_result(prefetched: PrefetchedToolExecution) -> ToolResult:
    try:
        return await prefetched.task
    except asyncio.CancelledError:
        return ToolResult(
            content=f"Prefetched tool '{prefetched.tool_call.name}' was cancelled before completion.",
            is_error=True,
            status="failed",
        )
    except Exception as exc:
        return ToolResult(content=f"Execution failed: {exc}", is_error=True, status="failed")


def cancel_prefetched_tool_executions(
    prefetched_results: dict[str, PrefetchedToolExecution] | None,
) -> None:
    if not prefetched_results:
        return
    for prefetched in prefetched_results.values():
        if not prefetched.task.done():
            prefetched.task.cancel()
    prefetched_results.clear()


def maybe_start_prefetched_tool_execution(
    raw_tc: ToolCallEvent,
    *,
    state: AgentState,
    tool_registry: ToolRegistry,
    permission_checker: PermissionChecker,
    permission_context: PermissionContext | None,
    tool_ctx: ToolExecutionContext,
    stagnation_limit: int,
    existing: dict[str, PrefetchedToolExecution],
    guardrail_controller: ToolCallGuardrailController | None = None,
) -> PrefetchedToolExecution | None:
    """Start a safe tool in the background as soon as its block is complete.

    This intentionally covers a conservative subset: concurrency-safe,
    auto-permitted, non-mutating tools. Open-world tools are excluded except
    MiniCode's built-in web read/search tools after permission policy has
    already classified the exact call as AUTO. Final UI/context projection
    still happens through execute_tool_batch, preserving ordering.
    """
    from backend.hooks import get_hook_manager
    from backend.hooks.manager import HookEvent

    hook_mgr = get_hook_manager()
    if hook_mgr is not None:
        has_hooks = getattr(hook_mgr, "has_hooks", None)
        if not callable(has_hooks) or has_hooks(HookEvent.PRE_TOOL_USE):
            return None
    tc = normalize_tool_call_event(raw_tc)
    if not tc.id or tc.id in existing:
        return None
    # Prefetch only calls whose arguments are already final. Otherwise the
    # normal executor may authorize and display repaired args while consuming a
    # result produced from the original args.
    if repair_tool_call_for_execution(state, tc, tool_registry, tool_ctx).tool_call != tc:
        return None
    if invalid_tool_call_guard_reason(tc, tool_registry):
        return None
    if model_toolset_guard(tc.name, tool_registry, getattr(tool_ctx, "metadata", None))[0]:
        return None
    if disabled_tool_guard_reason(state, tc):
        return None
    if repeated_similar_web_search_result(state, tc) is not None:
        return None
    if state.repeated_call_guard_reason(tc.name, tc.arguments, limit=stagnation_limit):
        return None
    if guardrail_controller is not None:
        try:
            decision = guardrail_controller.before_call(tc.name, tc.arguments)
        except Exception:
            decision = None
        if decision is not None and not decision.allows_execution:
            return None
    if missing_required_tool_argument_reason(state, tc, tool_registry):
        return None
    if missing_required_tool_argument_names(tc, tool_registry):
        return None

    tool = tool_registry.get_tool(tc.name)
    if tool is None or tc.name in SPECIAL_TOOL_NAMES:
        return None
    if getattr(tool, "open_world", False) and tc.name not in WEB_TOOL_NAMES:
        return None
    if _tool_mutates(tc.name, tool_registry):
        return None
    try:
        if not tool.is_concurrency_safe(tc.arguments):
            return None
    except Exception:
        return None
    try:
        validate_msg = tool.validate_input(tc.arguments)
    except Exception:
        validate_msg = ""
    if validate_msg:
        return None

    permission_decision = evaluate_permission_decision(
        permission_checker,
        tc.name,
        tc.arguments,
        context=permission_context,
        tool=tool,
    )
    if (
        permission_decision.decision != "allow"
        or not permission_decision.capability_allowed
        or permission_decision.permission_level != PermissionLevel.AUTO
    ):
        return None

    started_epoch = time.time()
    task = asyncio.create_task(
        run_tool_with_timeout(
            tc,
            tool_registry,
            tool_ctx,
            iteration_id=f"iter:{max(1, state.iterations)}",
        )
    )

    def _consume_unhandled_exception(done_task: asyncio.Task[ToolResult]) -> None:
        if done_task.cancelled():
            return
        try:
            done_task.exception()
        except Exception:
            pass

    task.add_done_callback(_consume_unhandled_exception)
    prefetched = PrefetchedToolExecution(
        tool_call=tc,
        task=task,
        started_epoch=started_epoch,
    )
    existing[tc.id] = prefetched
    return prefetched


class StreamingToolExecutor:
    """Starts complete safe tool blocks during model streaming.

    This mirrors the Claude Code boundary: a complete tool block may begin
    running before the provider's final message frame, while final projection
    and context writes remain ordered in ``execute_tool_batch``.
    """

    def __init__(
        self,
        *,
        state: AgentState,
        tool_registry: ToolRegistry,
        permission_checker: PermissionChecker,
        permission_context: PermissionContext | None,
        tool_ctx: ToolExecutionContext,
        stagnation_limit: int,
        guardrail_controller: ToolCallGuardrailController | None = None,
    ) -> None:
        self.state = state
        self.tool_registry = tool_registry
        self.permission_checker = permission_checker
        self.permission_context = permission_context
        self.tool_ctx = tool_ctx
        self.stagnation_limit = stagnation_limit
        self.guardrail_controller = guardrail_controller
        self.prefetched_results: dict[str, PrefetchedToolExecution] = {}
        self.blocked_by_order = False

    def add_tool(self, tool_call: ToolCallEvent) -> PrefetchedToolExecution | None:
        if self.blocked_by_order:
            return None
        tc_id = str(tool_call.id or "")
        if tc_id and tc_id in self.prefetched_results:
            return self.prefetched_results[tc_id]
        prefetched = maybe_start_prefetched_tool_execution(
            tool_call,
            state=self.state,
            tool_registry=self.tool_registry,
            permission_checker=self.permission_checker,
            permission_context=self.permission_context,
            tool_ctx=self.tool_ctx,
            stagnation_limit=self.stagnation_limit,
            existing=self.prefetched_results,
            guardrail_controller=self.guardrail_controller,
        )
        if prefetched is None:
            # Preserve model order: once a completed block is not safe to start
            # during streaming, later blocks wait for the normal executor.
            self.blocked_by_order = True
        return prefetched

    def add_tools(self, tool_calls: list[ToolCallEvent]) -> None:
        for tool_call in tool_calls:
            if self.add_tool(tool_call) is None:
                break

    def get_completed_results(self, ordered_tool_calls: list[ToolCallEvent]) -> list[PrefetchedToolExecution]:
        completed: list[PrefetchedToolExecution] = []
        for tool_call in ordered_tool_calls:
            prefetched = _matching_prefetch(
                self.prefetched_results,
                normalize_tool_call_event(tool_call),
            )
            if prefetched is None or not prefetched.task.done():
                break
            completed.append(prefetched)
        return completed

    def get_remaining_results(self, ordered_tool_calls: list[ToolCallEvent]) -> list[PrefetchedToolExecution]:
        completed_ids = {item.tool_call.id for item in self.get_completed_results(ordered_tool_calls)}
        remaining: list[PrefetchedToolExecution] = []
        for tool_call in ordered_tool_calls:
            prefetched = _matching_prefetch(
                self.prefetched_results,
                normalize_tool_call_event(tool_call),
            )
            if prefetched is not None and prefetched.tool_call.id not in completed_ids:
                remaining.append(prefetched)
        return remaining

    def cancel_remaining(self) -> None:
        cancel_prefetched_tool_executions(self.prefetched_results)


def invalid_tool_call_guard_reason(tc: ToolCallEvent, tool_registry: ToolRegistry) -> str:
    name = str(tc.name or "").strip()
    if not name:
        return (
            "Invalid tool call from model: missing tool name. "
            "Re-read the available tool schemas and retry with a valid tool name and required arguments."
        )
    if not isinstance(tc.arguments, dict):
        return (
            f"Invalid tool call for '{name}': arguments must be a JSON object. "
            "Retry with arguments that match the tool schema."
        )
    if (
        tool_registry.get_tool(name) is None
        and name not in SPECIAL_TOOL_NAMES
        and name not in INTERNAL_GUARDED_TOOL_NAMES
    ):
        available = ", ".join(tool_registry.list_tools()) or "none"
        return (
            f"Tool '{name}' does not exist. Available tools: {available}. "
            "Choose one of the available tools or answer without tools."
        )
    return ""


def model_toolset_guard(
    tool_name: str,
    tool_registry: ToolRegistry,
    metadata: dict[str, Any] | None,
) -> tuple[str, str]:
    coordinator_reason = coordinator_tool_block_reason(tool_name, metadata)
    if coordinator_reason:
        return coordinator_reason, "coordinator_allowlist"
    spec = tool_spec_for(tool_name, tool_registry)
    if spec.toolset == "coordinator" and not coordinator_mode_enabled(metadata):
        return (
            f"Tool '{tool_name}' is only available in coordinator mode. "
            "Use task, task_status, send_message, or task_stop from the default agent surface.",
            "coordinator_only",
        )
    return "", ""


async def snapshot_before_write(tc: ToolCallEvent, tool_ctx: ToolExecutionContext) -> None:
    if tc.name not in CHECKPOINT_WRITE_TOOL_NAMES:
        return
    manager = getattr(tool_ctx, "checkpoint_manager", None)
    if manager is None:
        return
    try:
        record = await manager.snapshot(
            tool_name=tc.name,
            args=tc.arguments,
            workspace_root=tool_ctx.workspace_root,
            conversation_id=tool_ctx.conversation_id,
            session_id=tool_ctx.session_id,
            tool_call_id=tc.id,
        )
    except Exception as exc:
        logger.warning("checkpoint snapshot failed: %s", exc)
        return
    if record is None:
        return
    emit = getattr(tool_ctx, "emit_event", None)
    if emit:
        try:
            await emit("checkpoint.created", record.to_dict())
        except Exception as exc:
            logger.debug("checkpoint emit failed: %s", exc)


async def run_tool(
    tc: ToolCallEvent,
    tool_registry: ToolRegistry,
    tool_ctx: ToolExecutionContext,
) -> ToolResult:
    from backend.hooks import get_hook_manager

    if getattr(tc, "_pre_tool_hook_applied", False):
        delattr(tc, "_pre_tool_hook_applied")
    else:
        pre_result = await _apply_pre_tool_hook(tc)
        if hasattr(tc, "_pre_tool_hook_applied"):
            delattr(tc, "_pre_tool_hook_applied")
        if pre_result is not None:
            return pre_result
    hook_mgr = get_hook_manager()

    # Validate required arguments against the same helper used by repair/history
    # safety so empty strings and empty containers are handled consistently.
    missing = missing_required_tool_argument_names(tc, tool_registry)
    if missing:
        reason = ToolArgRepairEngine(AgentState(user_message=""), tool_registry).missing_required_reason(tc)
        received = list(tc.arguments.keys())
        return ToolResult(
            content=reason or (
                f"Tool '{tc.name}' is missing required argument(s): {missing}. "
                f"Received keys: {received}. Re-read the tool schema and retry with all required fields."
            ),
            is_error=True,
        )

    await snapshot_before_write(tc, tool_ctx)
    changed_file = changed_file_event_payload(tc, tool_ctx)
    metadata_had_tool_id = "_current_tool_call_id" in tool_ctx.metadata
    previous_tool_id = tool_ctx.metadata.get("_current_tool_call_id")
    if tc.name in CHECKPOINT_WRITE_TOOL_NAMES:
        tool_ctx.metadata["_current_tool_call_id"] = tc.id

    tm = tool_ctx.task_manager
    try:
        if tm:
            try:
                managed = tm.create(
                    kind="tool_run",
                    awaitable=tool_registry.execute(tc.name, tc.arguments, context=tool_ctx),
                )
                result = await tm.wait(managed.id)
            except Exception as exc:
                result = ToolResult(content=f"Task execution failed: {exc}", is_error=True)
        else:
            result = await tool_registry.execute(tc.name, tc.arguments, context=tool_ctx)
    finally:
        if tc.name in CHECKPOINT_WRITE_TOOL_NAMES:
            if metadata_had_tool_id:
                tool_ctx.metadata["_current_tool_call_id"] = previous_tool_id
            else:
                tool_ctx.metadata.pop("_current_tool_call_id", None)

    if hook_mgr and not result.is_error:
        try:
            await hook_mgr.run_post_tool(tc.name, tc.arguments, result.content or "")
        except Exception as exc:
            logger.warning("post_tool hook failed: %s", exc)
    elif hook_mgr and result.is_error:
        try:
            await hook_mgr.run_post_tool_failure(tc.name, tc.arguments, result.content or "")
        except Exception as exc:
            logger.warning("post_tool_failure hook failed: %s", exc)

    if changed_file and not result.is_error:
        emit = getattr(tool_ctx, "emit_event", None)
        if emit:
            try:
                await emit("file.changed", changed_file)
            except Exception as exc:
                logger.debug("file change emit failed: %s", exc)
        if hook_mgr:
            try:
                await hook_mgr.run_file_changed(
                    str(changed_file.get("path") or ""),
                    event=str(changed_file.get("event") or "modified"),
                    tool_name=tc.name,
                    tool_call_id=tc.id,
                )
            except Exception as exc:
                logger.warning("file_changed hook failed: %s", exc)

    return result


async def _apply_pre_tool_hook(tc: ToolCallEvent) -> ToolResult | None:
    from backend.hooks import get_hook_manager

    tc.arguments = normalized_tool_arguments(tc.name, tc.arguments)
    hook_mgr = get_hook_manager()
    if hook_mgr is not None:
        try:
            pre = await hook_mgr.run_pre_tool(tc.name, tc.arguments)
            if pre.blocked:
                return ToolResult(content=f"Tool blocked by hook: {pre.message}", is_error=True)
            if isinstance(pre.updated_input, dict):
                tc.arguments = normalized_tool_arguments(tc.name, dict(pre.updated_input))
        except Exception as exc:
            logger.warning("pre_tool hook failed: %s", exc)
    setattr(tc, "_pre_tool_hook_applied", True)
    return None


def _display_path_for_tool_arg(raw_path: str, tool_ctx: ToolExecutionContext) -> tuple[Path, str]:
    workspace_root = Path(tool_ctx.workspace_root).resolve() if tool_ctx.workspace_root else None
    path = Path(raw_path)
    resolved = path.resolve() if path.is_absolute() else ((workspace_root / path).resolve() if workspace_root else path.resolve())
    display_path = raw_path
    if workspace_root:
        try:
            display_path = resolved.relative_to(workspace_root).as_posix()
        except ValueError:
            display_path = resolved.as_posix()
    return resolved, display_path


def changed_file_event_payload(
    tc: ToolCallEvent,
    tool_ctx: ToolExecutionContext,
) -> dict[str, Any] | None:
    if tc.name not in CHECKPOINT_WRITE_TOOL_NAMES:
        return None
    raw_path = str(tc.arguments.get("file_path") or "").strip()
    if not raw_path:
        return None
    resolved, display_path = _display_path_for_tool_arg(raw_path, tool_ctx)
    existed_before = resolved.exists()
    event_type = "created" if tc.name == "write_file" and not existed_before else "modified"
    return {
        "path": display_path,
        "event": event_type,
    }


async def run_tool_with_timeout(
    tc: ToolCallEvent,
    tool_registry: ToolRegistry,
    tool_ctx: ToolExecutionContext,
    *,
    iteration_id: str = "",
) -> ToolResult:
    timeout = _resolve_tool_timeout(tc.name, tool_registry)
    execution_tool_ctx = tool_context_with_live_output(tc, tool_ctx, iteration_id=iteration_id)
    t0 = time.perf_counter()
    try:
        async with asyncio.timeout(timeout):
            result = await run_tool(tc, tool_registry, execution_tool_ctx)
    except asyncio.TimeoutError:
        elapsed = int((time.perf_counter() - t0) * 1000)
        if is_non_critical_timeout_tool(tc.name):
            return ToolResult(
                content=(
                    f"Optional tool '{tc.name}' timed out after {timeout:.0f}s. "
                    "Do not retry it in this turn; continue with the user-facing answer."
                ),
                is_error=False,
                duration_ms=elapsed,
                status="timeout",
                limitation="non-critical timeout",
                display_summary=f"Optional tool timed out: {tc.name}",
                result_kind=result_kind_for_tool(tc.name),
            )
        return ToolResult(
            content=(
                f"Tool '{tc.name}' timed out after {timeout:.0f}s. "
                "The operation did not finish and no complete result is available. "
                "Do not retry the identical call; break the operation into smaller steps or try a different approach."
            ),
            is_error=True,
            duration_ms=elapsed,
            status="timeout",
            limitation="timeout",
            display_summary=f"Timed out: {tc.name}",
            result_kind=result_kind_for_tool(tc.name),
        )
    elapsed = int((time.perf_counter() - t0) * 1000)
    if result.duration_ms is None:
        result = replace(result, duration_ms=elapsed)
    return result


def is_non_critical_timeout_tool(name: str) -> bool:
    lower = name.lower()
    return lower in NON_CRITICAL_TIMEOUT_TOOLS


def tool_output_delta_events(
    tc: ToolCallEvent,
    result: ToolResult,
    *,
    turn_id: str = "",
    iteration_id: str = "",
) -> list[AgentEvent]:
    if result.is_error or tc.name not in COMMAND_OUTPUT_STREAM_TOOL_NAMES or not result.content:
        return []

    chunk_size = 2000
    output_text = result.content
    return [
        AgentEvent.tool_output_delta(
            id=tc.id,
            output=output_text[index:index + chunk_size],
            stream="stdout",
            turn_id=turn_id,
            iteration_id=iteration_id,
            step_id=tc.id,
        )
        for index in range(0, len(output_text), chunk_size)
    ]


def _tool_progress_event(
    tc: ToolCallEvent,
    message: str,
    *,
    phase: str,
    status: str = "running",
    visibility: str = "timeline",
    detail: str = "",
    iteration_id: str = "",
    count: int | None = None,
    display_scope: str = "activity",
    panel_hint: str = "inspector",
    requires_attention: bool = False,
) -> AgentEvent:
    return AgentEvent.progress(
        message,
        stage="tool" if phase != "approval" else "approval",
        status=status,
        id=f"{phase}:{tc.id}",
        phase=phase,
        label=tc.name,
        summary=message,
        detail=detail,
        visibility=visibility,
        tool_call_id=tc.id,
        tool_name=tc.name,
        group_id=iteration_id,
        step_id=tc.id,
        iteration_id=iteration_id,
        count=count,
        display_scope=display_scope,
        panel_hint=panel_hint,
        requires_attention=requires_attention,
    )


def _tool_preparing_event(tc: ToolCallEvent, *, iteration_id: str = "") -> AgentEvent:
    return _tool_progress_event(
        tc,
        f"Preparing {tc.name}",
        phase="tool",
        visibility="compact",
        iteration_id=iteration_id,
    )


def _tool_dispatched_event(
    tc: ToolCallEvent,
    *,
    iteration_id: str = "",
    count: int | None = None,
    parallel: bool = False,
) -> AgentEvent:
    detail = "Queued for parallel execution" if parallel else "Queued for execution"
    return _tool_progress_event(
        tc,
        f"Queued {tc.name}",
        phase="tool",
        visibility="compact",
        detail=detail,
        iteration_id=iteration_id,
        count=count,
    )


def _tool_started_event(
    tc: ToolCallEvent,
    *,
    iteration_id: str = "",
    detail: str = "",
    count: int | None = None,
) -> AgentEvent:
    return _tool_progress_event(
        tc,
        f"Running {tc.name}",
        phase="tool",
        iteration_id=iteration_id,
        detail=detail,
        count=count,
    )


def _tool_first_output_event(
    tc: ToolCallEvent,
    *,
    iteration_id: str = "",
    detail: str = "",
) -> AgentEvent:
    return _tool_progress_event(
        tc,
        f"Streaming output from {tc.name}",
        phase="tool",
        visibility="compact",
        detail=detail,
        iteration_id=iteration_id,
    )


def _tool_waiting_approval_event(
    tc: ToolCallEvent,
    *,
    iteration_id: str = "",
    detail: str = "",
) -> AgentEvent:
    return _tool_progress_event(
        tc,
        f"Waiting for approval: {tc.name}",
        phase="approval",
        detail=detail or "User approval required before execution",
        iteration_id=iteration_id,
        display_scope="activity",
        panel_hint="diff",
        requires_attention=True,
    )


def _emit_runtime_event(tool_ctx: ToolExecutionContext, event: AgentEvent) -> Awaitable[None] | None:
    emit_event = getattr(tool_ctx, "emit_event", None)
    if emit_event is None:
        return None
    return emit_event(event.type, dict(event.data))


def _tool_runtime_span_event(
    tc: ToolCallEvent,
    tool_ctx: ToolExecutionContext,
    *,
    event: str,
    iteration_id: str = "",
    phase: str = "tool",
    status: str = "running",
    summary: str = "",
    detail: str = "",
    waiting_on: str = "",
    blocking_reason: str = "",
    requires_attention: bool = False,
    ui_visible: bool = True,
    duration_ms: int | None = None,
    data: dict[str, Any] | None = None,
) -> AgentEvent:
    payload_data = dict(data or {})
    if detail:
        payload_data["detail"] = detail
    return runtime_span_from_tool_context(
        event,
        span_id=f"tool:{tc.id}",
        tool_ctx=tool_ctx,
        iteration_id=iteration_id,
        phase=phase,
        status=status,
        label=tc.name,
        summary=summary,
        tool_call_id=tc.id,
        tool_name=tc.name,
        waiting_on=waiting_on,
        blocking_reason=blocking_reason,
        requires_attention=requires_attention,
        ui_visible=ui_visible,
        duration_ms=duration_ms,
        data=payload_data or None,
    )


async def _emit_tool_runtime_span(
    tc: ToolCallEvent,
    tool_ctx: ToolExecutionContext,
    *,
    event: str,
    iteration_id: str = "",
    phase: str = "tool",
    status: str = "running",
    summary: str = "",
    detail: str = "",
    waiting_on: str = "",
    blocking_reason: str = "",
    requires_attention: bool = False,
    ui_visible: bool = True,
    duration_ms: int | None = None,
    data: dict[str, Any] | None = None,
) -> None:
    pending = _emit_runtime_event(
        tool_ctx,
        _tool_runtime_span_event(
            tc,
            tool_ctx,
            event=event,
            iteration_id=iteration_id,
            phase=phase,
            status=status,
            summary=summary,
            detail=detail,
            waiting_on=waiting_on,
            blocking_reason=blocking_reason,
            requires_attention=requires_attention,
            ui_visible=ui_visible,
            duration_ms=duration_ms,
            data=data,
        ),
    )
    if pending is not None:
        await pending


async def _emit_tool_completed_runtime_span(
    tc: ToolCallEvent,
    tool_ctx: ToolExecutionContext,
    result_event: AgentEvent,
    *,
    iteration_id: str = "",
) -> None:
    payload = result_event.data if isinstance(result_event.data, dict) else {}
    final_status = str(payload.get("status") or "").strip().lower()
    failed = final_status in {"failed", "blocked", "timeout"} or bool(payload.get("is_error"))
    duration_value = payload.get("duration_ms")
    duration_ms = int(duration_value) if isinstance(duration_value, int) else None
    summary = str(
        payload.get("display_summary")
        or payload.get("user_summary")
        or payload.get("summary")
        or ("Failed " + tc.name if failed else "Completed " + tc.name)
    )
    await _emit_tool_runtime_span(
        tc,
        tool_ctx,
        event="tool.completed",
        iteration_id=iteration_id,
        phase="tool",
        status="failed" if failed else "completed",
        summary=summary,
        detail=str(payload.get("limitation") or ""),
        requires_attention=bool(payload.get("requires_attention")),
        duration_ms=duration_ms,
        data={
            "tool_status": final_status,
            "result_kind": payload.get("result_kind") or "",
            "projection": payload.get("projection") or "",
        },
    )


async def _emit_tool_first_output_progress(
    tc: ToolCallEvent,
    tool_ctx: ToolExecutionContext,
    *,
    iteration_id: str = "",
    detail: str = "",
) -> None:
    first_output_key = "_tool_first_output_ids"
    raw_ids = tool_ctx.metadata.setdefault(first_output_key, set())
    if isinstance(raw_ids, set):
        if tc.id in raw_ids:
            return
        raw_ids.add(tc.id)
    elif isinstance(raw_ids, list):
        if tc.id in raw_ids:
            return
        raw_ids.append(tc.id)
    else:
        tool_ctx.metadata[first_output_key] = {tc.id}
    pending = _emit_runtime_event(
        tool_ctx,
        _tool_first_output_event(tc, iteration_id=iteration_id, detail=detail),
    )
    if pending is not None:
        await pending
    await _emit_tool_runtime_span(
        tc,
        tool_ctx,
        event="tool.first_output",
        iteration_id=iteration_id,
        phase="tool",
        status="running",
        summary=f"Streaming output from {tc.name}",
        detail=detail,
    )


_STREAMED_TOOL_OUTPUT_IDS_KEY = "_streamed_tool_output_ids"


def _mark_tool_output_streamed(tool_ctx: ToolExecutionContext, tool_call_id: str) -> None:
    raw_ids = tool_ctx.metadata.setdefault(_STREAMED_TOOL_OUTPUT_IDS_KEY, set())
    if isinstance(raw_ids, set):
        raw_ids.add(tool_call_id)
        return
    if isinstance(raw_ids, list):
        raw_ids.append(tool_call_id)
        return
    tool_ctx.metadata[_STREAMED_TOOL_OUTPUT_IDS_KEY] = {tool_call_id}


def _tool_output_was_streamed(tool_ctx: ToolExecutionContext, tool_call_id: str) -> bool:
    raw_ids = tool_ctx.metadata.get(_STREAMED_TOOL_OUTPUT_IDS_KEY)
    return isinstance(raw_ids, (set, list, tuple)) and tool_call_id in raw_ids


def tool_context_with_live_output(
    tc: ToolCallEvent,
    tool_ctx: ToolExecutionContext,
    *,
    iteration_id: str = "",
) -> ToolExecutionContext:
    if tc.name not in COMMAND_OUTPUT_STREAM_TOOL_NAMES:
        return tool_ctx
    emit_event = getattr(tool_ctx, "emit_event", None)
    fallback_stream = getattr(tool_ctx, "stream_callback", None)
    if emit_event is None and fallback_stream is None:
        return tool_ctx

    async def _stream_tool_output(output: str, stream: str = "stdout") -> None:
        if not output:
            return
        stream_name = stream if stream in {"stdout", "stderr"} else "stdout"
        await _emit_tool_first_output_progress(
            tc,
            tool_ctx,
            iteration_id=iteration_id,
            detail=f"Received {stream_name} output",
        )
        _mark_tool_output_streamed(tool_ctx, tc.id)
        if emit_event is not None:
            payload = {
                "id": tc.id,
                "output": output,
                "stream": stream_name,
                "iteration_id": iteration_id,
                "step_id": tc.id,
            }
            turn_id = _tool_turn_id(tool_ctx)
            if turn_id:
                payload["turn_id"] = turn_id
            await emit_event(
                "tool_output_delta",
                payload,
            )
            return
        if fallback_stream is not None:
            try:
                await fallback_stream(output, stream_name)
            except TypeError:
                await fallback_stream(output)

    return replace(tool_ctx, stream_callback=_stream_tool_output)


def batch_tool_calls(
    tool_calls: list[ToolCallEvent],
    tool_registry: ToolRegistry,
) -> list[tuple[bool, list[ToolCallEvent]]]:
    """Group tool calls for parallel execution, preserving model order.

    Only *consecutive* concurrency-safe tools are batched together (cc's
    partitionToolCalls pattern). A mutating tool that sits between two reads is
    NOT reordered past them: `read A → edit B → read C` stays in that order, so
    a read that depends on an earlier edit's result cannot run before it. Each
    maximal run of adjacent safe tools becomes one parallel batch; every other
    call starts its own batch.
    """
    if not tool_calls:
        return []
    batches: list[tuple[bool, list[ToolCallEvent]]] = []
    for tc in tool_calls:
        tool = tool_registry.get_tool(tc.name)
        is_safe = tool.is_concurrency_safe(tc.arguments) if tool else False
        if is_safe and batches and batches[-1][0]:
            batches[-1][1].append(tc)
        else:
            batches.append((is_safe, [tc]))
    return batches


def _idempotent_call_signature(
    tc: ToolCallEvent,
    tool_registry: ToolRegistry,
) -> ToolCallSignature | None:
    if not _tool_is_idempotent(tc.name, tool_registry, tc.arguments):
        return None
    return ToolCallSignature.from_call(tc.name, tc.arguments)


def _dedupe_idempotent_batch(
    batch: list[ToolCallEvent],
    tool_registry: ToolRegistry,
) -> tuple[list[ToolCallEvent], dict[str, list[ToolCallEvent]]]:
    """Return executable calls and duplicate calls keyed by source call id."""
    executable: list[ToolCallEvent] = []
    seen: dict[ToolCallSignature, ToolCallEvent] = {}
    duplicates_by_source_id: dict[str, list[ToolCallEvent]] = {}
    for tc in batch:
        signature = _idempotent_call_signature(tc, tool_registry)
        if signature is None:
            executable.append(tc)
            continue
        source = seen.get(signature)
        if source is None:
            seen[signature] = tc
            executable.append(tc)
            continue
        duplicates_by_source_id.setdefault(source.id, []).append(tc)
    return executable, duplicates_by_source_id


def _duplicate_idempotent_result(
    duplicate: ToolCallEvent,
    source: ToolCallEvent,
) -> ToolResult:
    return ToolResult(
        content=(
            f"Skipped duplicate tool call '{duplicate.name}' because this batch "
            f"already executed the same idempotent call as tool_call_id '{source.id}'. "
            "Use that immediately preceding tool result; do not repeat the same call."
        ),
        is_error=False,
        status="skipped",
        limitation="duplicate_idempotent_call",
        display_summary=f"Duplicate tool call skipped: {duplicate.name}",
        result_kind=result_kind_for_tool(duplicate.name),
    )


async def _finalize_tool_result(
    tc: ToolCallEvent,
    result: ToolResult,
    *,
    ctx: ContextBuilder,
    state: AgentState,
    tool_ctx: ToolExecutionContext,
    iteration_id: str,
    turn_id: str,
    status: str | None = None,
    diff: dict[str, Any] | None = None,
    append_to_context: bool = True,
    guardrail_controller: ToolCallGuardrailController | None = None,
    tool_registry: ToolRegistry | None = None,
) -> AsyncIterator[AgentEvent]:
    """Persist and emit one terminal result for every tool exit path."""
    events = store_result_events(
        tc,
        result,
        ctx,
        state,
        status=status,
        diff=diff,
        iteration_id=iteration_id,
        turn_id=turn_id,
        append_to_context=append_to_context,
        guardrail_controller=guardrail_controller,
        tool_ctx=tool_ctx,
        tool_registry=tool_registry,
    )
    for event in events:
        yield event
    if events:
        await _emit_tool_completed_runtime_span(
            tc,
            tool_ctx,
            events[-1],
            iteration_id=iteration_id,
        )


async def _reject_tool_call(
    tc: ToolCallEvent,
    auto_queue: list[ToolCallEvent],
    result: ToolResult,
    *,
    runtime: _ToolBatchRuntime,
    started_epoch: float | None = None,
    status: str = "blocked",
    append_to_context: bool = True,
    prefetched_results: dict[str, PrefetchedToolExecution] | None = None,
) -> AsyncIterator[AgentEvent]:
    """Flush pending auto-queue, emit a blocked tool-call event, and store the result."""
    async for ev in _flush_queue(
        auto_queue,
        ctx=runtime.ctx,
        state=runtime.state,
        tool_registry=runtime.tool_registry,
        tool_ctx=runtime.tool_ctx,
        iteration_id=runtime.iteration_id,
        guardrail_controller=runtime.guardrail_controller,
        prefetched_results=prefetched_results,
    ):
        yield ev
    auto_queue.clear()
    epoch = started_epoch if started_epoch is not None else time.time()
    yield tool_call_start_event(
        tc,
        started_epoch=epoch,
        iteration_id=runtime.iteration_id,
        tool_registry=runtime.tool_registry,
        turn_id=runtime.turn_id,
    )
    async for event in _finalize_tool_result(
        tc,
        result,
        ctx=runtime.ctx,
        state=runtime.state,
        status=status,
        diff=None,
        iteration_id=runtime.iteration_id,
        turn_id=runtime.turn_id,
        append_to_context=append_to_context,
        guardrail_controller=runtime.guardrail_controller,
        tool_ctx=runtime.tool_ctx,
    ):
        yield event


async def execute_tool_batch(
    tool_calls: list[ToolCallEvent],
    *,
    ctx: ContextBuilder,
    state: AgentState,
    tool_registry: ToolRegistry,
    permission_checker: PermissionChecker,
    approval_handler: Callable | None,
    skill_manager: Any | None,
    permission_context: PermissionContext | None,
    tool_ctx: ToolExecutionContext,
    stagnation_limit: int,
    guardrail_controller: ToolCallGuardrailController | None = None,
    prefetched_results: dict[str, PrefetchedToolExecution] | None = None,
    prepared_repair_results: list[RepairResult] | None = None,
) -> AsyncIterator[AgentEvent]:
    auto_queue: list[ToolCallEvent] = []
    iteration_id = f"iter:{max(1, state.iterations)}"
    runtime = _ToolBatchRuntime(
        ctx=ctx,
        state=state,
        tool_registry=tool_registry,
        tool_ctx=tool_ctx,
        iteration_id=iteration_id,
        turn_id=_tool_turn_id(tool_ctx),
        guardrail_controller=guardrail_controller,
    )
    prepared_repairs = (
        list(prepared_repair_results)
        if prepared_repair_results is not None
        else prepare_tool_call_sequence(state, tool_calls, tool_registry, tool_ctx)
    )

    for index, prepared_repair in enumerate(prepared_repairs, 1):
        tc = normalize_tool_call_event(
            prepared_repair.tool_call,
            fallback_id=f"tool_{index}",
        )
        repair_result = (
            prepared_repair
            if prepared_repair.tool_call == tc
            else replace(prepared_repair, tool_call=tc)
        )
        wrapped_tc = tc
        tc, bridge_block_reason = unwrap_deferred_tool_call(tc, tool_registry, permission_context)
        if bridge_block_reason:
            started_epoch = time.time()
            _tool_start_times(state)[tc.id] = started_epoch
            async for ev in _reject_tool_call(
                tc, auto_queue,
                _rejection_result(
                    tc,
                    bridge_block_reason,
                    display_summary="Deferred tool call blocked",
                    result_kind="generic",
                ),
                runtime=runtime,
                started_epoch=started_epoch,
                append_to_context=False,
                prefetched_results=prefetched_results,
            ):
                yield ev
            continue
        if tool_call_needs_list_context(tc, tool_registry) and auto_queue and not inferred_read_file_path_from_recent_list(state):
            async for ev in _flush_queue(
                auto_queue,
                ctx=ctx,
                state=state,
                tool_registry=tool_registry,
                tool_ctx=tool_ctx,
                iteration_id=iteration_id,
                guardrail_controller=guardrail_controller,
                prefetched_results=prefetched_results,
            ):
                yield ev
            auto_queue = []
            repair_result = repair_tool_call_for_execution(state, tc, tool_registry, tool_ctx)
        elif tc != wrapped_tc:
            # Deferred wrappers are prepared as the wrapper schema; repair the
            # newly unwrapped underlying call once before execution.
            repair_result = repair_tool_call_for_execution(state, tc, tool_registry, tool_ctx)
        tc = repair_result.tool_call

        prefetched = None
        started_epoch = time.time()
        _tool_start_times(state)[tc.id] = started_epoch

        if repair_result.needs_model_generation or repair_result.routing_correction:
            started_epoch_local = started_epoch
            async for ev in _reject_tool_call(
                tc, auto_queue,
                _rejection_result(
                    tc,
                    repair_result.model_observation,
                    is_error=False,
                    display_summary=(
                        "Missing generated content"
                        if repair_result.needs_model_generation
                        else "Routing correction"
                    ),
                ),
                runtime=runtime,
                started_epoch=started_epoch_local,
                append_to_context=False,
                prefetched_results=prefetched_results,
            ):
                yield ev
            state.add_loop_guidance(repair_result.model_observation)
            continue

        if repair_result.repaired and repair_result.model_observation:
            state.add_loop_guidance(repair_result.model_observation)

        pre_tool_result = await _apply_pre_tool_hook(tc)
        if pre_tool_result is not None:
            async for ev in _reject_tool_call(
                tc,
                auto_queue,
                pre_tool_result,
                runtime=runtime,
                started_epoch=started_epoch,
                status="blocked",
                prefetched_results=prefetched_results,
            ):
                yield ev
            continue

        prefetched = _matching_prefetch(prefetched_results, tc)
        if prefetched is not None:
            started_epoch = prefetched.started_epoch
            _tool_start_times(state)[tc.id] = started_epoch

        invalid_reason = invalid_tool_call_guard_reason(tc, tool_registry)

        repeat_reason = state.repeated_call_guard_reason(tc.name, tc.arguments, limit=stagnation_limit)
        if repeat_reason:
            async for ev in _reject_tool_call(
                tc, auto_queue,
                _rejection_result(
                    tc, repeat_reason,
                    is_error=False,
                    display_summary="Already attempted",
                ),
                runtime=runtime,
                started_epoch=started_epoch,
                prefetched_results=prefetched_results,
            ):
                yield ev
            continue

        similar_web_search_result = repeated_similar_web_search_result(state, tc)
        if similar_web_search_result is not None:
            async for ev in _reject_tool_call(
                tc,
                auto_queue,
                similar_web_search_result,
                runtime=runtime,
                started_epoch=started_epoch,
                status="success",
                prefetched_results=prefetched_results,
            ):
                yield ev
            continue

        duplicate_output_result = duplicate_output_write_guard_result(state, tc)
        if duplicate_output_result is not None:
            async for ev in _reject_tool_call(
                tc,
                auto_queue,
                duplicate_output_result,
                runtime=runtime,
                started_epoch=started_epoch,
                status="blocked",
                prefetched_results=prefetched_results,
            ):
                yield ev
            continue

        guardrail_result = guardrail_before_call_result(guardrail_controller, tc)
        if guardrail_result is not None:
            async for ev in _reject_tool_call(
                tc, auto_queue,
                guardrail_result,
                runtime=runtime,
                started_epoch=started_epoch,
                prefetched_results=prefetched_results,
            ):
                yield ev
            continue

        if invalid_reason:
            async for ev in _reject_tool_call(
                tc, auto_queue,
                _rejection_result(
                    tc,
                    invalid_reason,
                    display_summary="Invalid tool call",
                    result_kind="generic",
                ),
                runtime=runtime,
                started_epoch=started_epoch,
                append_to_context=False,
                prefetched_results=prefetched_results,
            ):
                yield ev
            continue

        repair_block_reason = repair_result_block_reason(state, repair_result, tool_registry)
        if repair_block_reason:
            malformed_web_tool_call = is_malformed_web_tool_call(repair_block_reason)
            async for ev in _reject_tool_call(
                tc, auto_queue,
                _invalid_call_result(
                    tc,
                    repair_block_reason,
                    malformed_web_call=malformed_web_tool_call,
                ),
                runtime=runtime,
                started_epoch=started_epoch,
                append_to_context=False,
                prefetched_results=prefetched_results,
            ):
                yield ev
            continue

        disabled_reason = disabled_tool_guard_reason(state, tc)
        if disabled_reason:
            history_safe = tool_call_is_safe_for_model_history(tc, tool_registry)
            malformed_disabled_web_call = tc.name in WEB_TOOL_NAMES and not history_safe
            async for ev in _reject_tool_call(
                tc, auto_queue,
                _rejection_result(
                    tc,
                    disabled_reason,
                    is_error=malformed_disabled_web_call,
                    display_summary="Invalid web tool call" if malformed_disabled_web_call else "Tool disabled",
                    result_kind="search" if tc.name in WEB_SEARCH_TOOL_NAMES else result_kind_for_tool(tc.name),
                ),
                runtime=runtime,
                started_epoch=started_epoch,
                status="blocked" if malformed_disabled_web_call else "success",
                append_to_context=history_safe,
                prefetched_results=prefetched_results,
            ):
                yield ev
            continue

        toolset_block_reason, toolset_rule = model_toolset_guard(
            tc.name,
            tool_registry,
            getattr(tool_ctx, "metadata", None),
        )
        if toolset_block_reason:
            yield AgentEvent.permission_decision(
                tool_call_id=tc.id,
                tool_name=tc.name,
                decision="deny",
                source="policy",
                permission_level=PermissionLevel.ALWAYS_DENY.value,
                message=toolset_block_reason,
                capability={"allowed": False, "reason": toolset_block_reason},
                approval_policy="deny",
                matched_rule={"source": "toolset_policy", "rule": toolset_rule},
                risk="high",
                scope={
                    "workspace_scope": tool_ctx.permission.workspace_scope,
                    "boundary": "agent_mode",
                },
                expiry="policy",
            )
            async for ev in _reject_tool_call(
                tc,
                auto_queue,
                _rejection_result(
                    tc,
                    toolset_block_reason,
                    display_summary="Coordinator tool blocked",
                    result_kind="subagent",
                ),
                runtime=runtime,
                started_epoch=started_epoch,
                status="blocked",
                prefetched_results=prefetched_results,
            ):
                yield ev
            continue

        coordinator_delegation_reason = coordinator_delegation_block_reason(
            tc.name,
            tc.arguments,
            getattr(tool_ctx, "metadata", None),
            state=state,
        )
        if coordinator_delegation_reason:
            async for ev in _reject_tool_call(
                tc,
                auto_queue,
                _rejection_result(
                    tc,
                    coordinator_delegation_reason,
                    display_summary="Coordinator delegation blocked",
                    result_kind="subagent",
                ),
                runtime=runtime,
                started_epoch=started_epoch,
                status="blocked",
                prefetched_results=prefetched_results,
            ):
                yield ev
            continue

        scope_guard_reason = subagent_scope_guard_reason(tc, tool_registry, tool_ctx)
        if scope_guard_reason:
            yield AgentEvent.permission_decision(
                tool_call_id=tc.id,
                tool_name=tc.name,
                decision="deny",
                source="policy",
                permission_level=PermissionLevel.ALWAYS_DENY.value,
                message=scope_guard_reason,
                capability={"allowed": False, "reason": scope_guard_reason},
                approval_policy="deny",
                matched_rule={"source": "subagent_scope", "rule": "write_scope"},
                risk="high",
                scope={
                    "workspace_scope": tool_ctx.permission.workspace_scope,
                    "boundary": "subagent_scope",
                    "task_id": tool_ctx.task_id,
                },
                expiry="policy",
            )
            async for ev in _reject_tool_call(
                tc,
                auto_queue,
                _rejection_result(
                    tc,
                    scope_guard_reason,
                    display_summary="Subagent scope blocked",
                    result_kind=result_kind_for_tool(tc.name),
                ),
                runtime=runtime,
                started_epoch=started_epoch,
                status="blocked",
                prefetched_results=prefetched_results,
            ):
                yield ev
            continue

        required_reason = missing_required_tool_argument_reason(state, tc, tool_registry)
        if required_reason:
            malformed_web_tool_call = is_malformed_web_tool_call(required_reason)
            async for ev in _reject_tool_call(
                tc, auto_queue,
                _invalid_call_result(
                    tc,
                    required_reason,
                    malformed_web_call=malformed_web_tool_call,
                ),
                runtime=runtime,
                started_epoch=started_epoch,
                append_to_context=False,
                prefetched_results=prefetched_results,
            ):
                yield ev
            continue

        command_file_write_reason = (
            run_command_file_write_guard_reason(str(tc.arguments.get("command") or ""))
            if tc.name == "run_command"
            else ""
        )
        if command_file_write_reason:
            async for ev in _reject_tool_call(
                tc, auto_queue,
                _rejection_result(tc, command_file_write_reason),
                runtime=runtime,
                started_epoch=started_epoch,
                prefetched_results=prefetched_results,
            ):
                yield ev
            continue

        perm_tool = tool_registry.get_tool(tc.name)

        # Tool-owned input validation (Phase 4.1 / CC validateInput): runs after
        # schema/guard checks and before permission. A non-empty message blocks
        # execution and is surfaced as an observation so the model can correct.
        if perm_tool is not None:
            try:
                validate_msg = perm_tool.validate_input(tc.arguments)
            except Exception:
                validate_msg = ""
            if validate_msg:
                async for ev in _reject_tool_call(
                    tc, auto_queue,
                    _rejection_result(
                        tc, validate_msg,
                        display_summary="Invalid tool input",
                        result_kind="generic",
                    ),
                    runtime=runtime,
                    started_epoch=started_epoch,
                    append_to_context=False,
                    prefetched_results=prefetched_results,
                ):
                    yield ev
                continue

        permission_decision = evaluate_permission_decision(
            permission_checker,
            tc.name,
            tc.arguments,
            context=permission_context,
            tool=perm_tool,
        )
        perm = permission_decision.permission_level
        denial = (
            permission_decision.capability_reason
            if not permission_decision.capability_allowed
            else permission_decision.matched_rule
            if permission_decision.decision == "deny"
            else None
        )
        yield AgentEvent.permission_decision(
            tool_call_id=tc.id,
            tool_name=tc.name,
            decision=permission_decision.decision,
            source="policy",
            permission_level=perm.value,
            message=(
                permission_decision.capability_reason
                if not permission_decision.capability_allowed
                else f"Denied by {permission_decision.matched_rule_source}: {permission_decision.matched_rule}"
                if permission_decision.decision == "deny"
                else ""
            ),
            capability={
                "allowed": permission_decision.capability_allowed,
                "reason": permission_decision.capability_reason,
            },
            approval_policy=permission_decision.approval_policy,
            matched_rule={
                "source": permission_decision.matched_rule_source,
                "rule": permission_decision.matched_rule,
            },
            risk=permission_decision.risk,
            scope=permission_decision.scope,
            expiry=permission_decision.expiry,
        )

        if denial or perm == PermissionLevel.ALWAYS_DENY:
            msg = denial or f"Tool '{tc.name}' is blocked by policy"
            from backend.hooks import get_hook_manager

            hook_mgr = get_hook_manager()
            if hook_mgr:
                try:
                    await hook_mgr.run_permission_denied(
                        tc.name,
                        tc.arguments,
                        reason=msg,
                        permission_level=perm.value,
                    )
                except Exception as exc:
                    logger.warning("permission_denied hook failed: %s", exc)
            async for ev in _reject_tool_call(
                tc, auto_queue,
                _rejection_result(tc, msg),
                runtime=runtime,
                started_epoch=started_epoch,
                prefetched_results=prefetched_results,
            ):
                yield ev
            continue

        # All guards passed — emit tool_call event to UI (now shows only real executions)
        yield tool_call_start_event(
            tc,
            started_epoch=started_epoch,
            iteration_id=iteration_id,
            tool_registry=tool_registry,
            turn_id=runtime.turn_id,
        )
        yield _tool_preparing_event(tc, iteration_id=iteration_id)
        await _emit_tool_runtime_span(
            tc,
            tool_ctx,
            event="tool.preparing",
            iteration_id=iteration_id,
            phase="tool",
            status="running",
            summary=f"Preparing {tc.name}",
            ui_visible=False,
        )

        if perm == PermissionLevel.AUTO and tc.name not in SPECIAL_TOOL_NAMES:
            yield _tool_dispatched_event(tc, iteration_id=iteration_id, parallel=True)
            await _emit_tool_runtime_span(
                tc,
                tool_ctx,
                event="tool.queued",
                iteration_id=iteration_id,
                phase="tool",
                status="running",
                summary=f"Queued {tc.name}",
                detail="Queued for parallel execution",
                ui_visible=False,
            )
            auto_queue.append(tc)
            continue

        async for ev in _flush_queue(
            auto_queue,
            ctx=ctx,
            state=state,
            tool_registry=tool_registry,
            tool_ctx=tool_ctx,
            iteration_id=iteration_id,
            guardrail_controller=guardrail_controller,
            prefetched_results=prefetched_results,
        ):
            yield ev
        auto_queue = []

        async for ev in _execute_serial(
            tc,
            perm=perm,
            ctx=ctx,
            state=state,
            tool_registry=tool_registry,
            tool_ctx=tool_ctx,
            approval_handler=approval_handler,
            skill_manager=skill_manager,
            iteration_id=iteration_id,
            guardrail_controller=guardrail_controller,
            prefetched=prefetched,
        ):
            yield ev

    async for ev in _flush_queue(auto_queue, ctx=ctx, state=state, tool_registry=tool_registry, tool_ctx=tool_ctx, iteration_id=iteration_id, guardrail_controller=guardrail_controller, prefetched_results=prefetched_results):
        yield ev


async def _flush_queue(
    queue: list[ToolCallEvent],
    *,
    ctx: ContextBuilder,
    state: AgentState,
    tool_registry: ToolRegistry,
    tool_ctx: ToolExecutionContext,
    iteration_id: str = "",
    guardrail_controller: ToolCallGuardrailController | None = None,
    prefetched_results: dict[str, PrefetchedToolExecution] | None = None,
) -> AsyncIterator[AgentEvent]:
    if not queue:
        return
    turn_id = _tool_turn_id(tool_ctx)

    for is_concurrent, batch in batch_tool_calls(queue, tool_registry):
        if is_concurrent and len(batch) > 1:
            diffs_by_id = {
                tc.id: generate_diff(
                    tc.name,
                    tc.arguments,
                    workspace_root=tool_ctx.workspace_root,
                    tool_ctx=tool_ctx,
                )
                for tc in batch
                if tc.name in CHECKPOINT_WRITE_TOOL_NAMES
            }
            executable_batch, duplicates_by_source_id = _dedupe_idempotent_batch(batch, tool_registry)
            # Sibling abort (Claude Code pattern): if a bash/command tool
            # fails, cancel all other parallel tools in the same batch.
            cancelled_result = ToolResult(
                content="Cancelled: parallel tool call errored.",
                is_error=True,
                status="failed",
            )

            async def _run_parallel_tool(tc: ToolCallEvent) -> ToolResult:
                similar_web_search_result = repeated_similar_web_search_result(state, tc)
                if similar_web_search_result is not None:
                    return similar_web_search_result
                prefetched = _take_matching_prefetch(prefetched_results, tc)
                if prefetched is not None:
                    return await _await_prefetched_result(prefetched)
                try:
                    return await run_tool_with_timeout(tc, tool_registry, tool_ctx, iteration_id=iteration_id)
                except Exception as exc:
                    return ToolResult(
                        content=f"Execution failed: {exc}",
                        is_error=True,
                        status="failed",
                    )

            max_concurrent_tools = max(1, min(len(executable_batch), _resolve_max_concurrent_tools()))
            batch_timeout = _resolve_tool_batch_timeout(batch, tool_registry)
            batch_deadline = time.monotonic() + batch_timeout
            pending: dict[asyncio.Task[ToolResult], ToolCallEvent] = {}
            results_by_id: dict[str, ToolResult] = {}
            next_executable_index = 0
            next_emit_index = 0
            batch_timed_out = False

            def _record_batch_result(tc: ToolCallEvent, result: ToolResult) -> None:
                results_by_id[tc.id] = result
                for duplicate in duplicates_by_source_id.get(tc.id, []):
                    results_by_id[duplicate.id] = _duplicate_idempotent_result(duplicate, tc)

            async def _start_ready_tasks() -> None:
                nonlocal next_executable_index
                while next_executable_index < len(executable_batch) and len(pending) < max_concurrent_tools:
                    tc = executable_batch[next_executable_index]
                    detail = f"Parallel batch {next_executable_index + 1}/{len(executable_batch)}"
                    pending_start = _emit_runtime_event(
                        tool_ctx,
                        _tool_started_event(
                            tc,
                            iteration_id=iteration_id,
                            detail=detail,
                            count=next_executable_index + 1,
                        ),
                    )
                    if pending_start is not None:
                        await pending_start
                    await _emit_tool_runtime_span(
                        tc,
                        tool_ctx,
                        event="tool.started",
                        iteration_id=iteration_id,
                        phase="tool",
                        status="running",
                        summary=f"Running {tc.name}",
                        detail=detail,
                        data={"parallel_index": next_executable_index + 1, "parallel_total": len(executable_batch)},
                    )
                    pending[asyncio.create_task(_run_parallel_tool(tc))] = tc
                    next_executable_index += 1

            def _pop_ready_ordered_results() -> list[tuple[ToolCallEvent, ToolResult]]:
                nonlocal next_emit_index
                ready: list[tuple[ToolCallEvent, ToolResult]] = []
                while next_emit_index < len(batch):
                    tc = batch[next_emit_index]
                    result = results_by_id.pop(tc.id, None)
                    if result is None:
                        break
                    ready.append((tc, result))
                    next_emit_index += 1
                return ready

            async def _emit_ordered_ready_results() -> AsyncIterator[AgentEvent]:
                for ready_tc, ready_result in _pop_ready_ordered_results():
                    if not _tool_output_was_streamed(tool_ctx, ready_tc.id):
                        if ready_tc.name in COMMAND_OUTPUT_STREAM_TOOL_NAMES and ready_result.content and not ready_result.is_error:
                            await _emit_tool_first_output_progress(
                                ready_tc,
                                tool_ctx,
                                iteration_id=iteration_id,
                                detail="Buffered command output available",
                            )
                        for event in tool_output_delta_events(
                            ready_tc,
                            ready_result,
                            turn_id=turn_id,
                            iteration_id=iteration_id,
                        ):
                            yield event
                    async for event in _finalize_tool_result(
                        ready_tc,
                        ready_result,
                        ctx=ctx,
                        state=state,
                        diff=None if ready_result.is_error else diffs_by_id.get(ready_tc.id),
                        iteration_id=iteration_id,
                        turn_id=turn_id,
                        guardrail_controller=guardrail_controller,
                        tool_ctx=tool_ctx,
                        tool_registry=tool_registry,
                    ):
                        yield event

            try:
                await _start_ready_tasks()
                while pending:
                    remaining = batch_deadline - time.monotonic()
                    if remaining <= 0:
                        batch_timed_out = True
                        break
                    done, _ = await asyncio.wait(
                        pending.keys(),
                        return_when=asyncio.FIRST_COMPLETED,
                        timeout=remaining,
                    )
                    if not done:
                        batch_timed_out = True
                        break
                    should_cancel_siblings = False

                    for task in done:
                        tc = pending.pop(task)
                        try:
                            result = task.result()
                        except asyncio.CancelledError:
                            result = cancelled_result
                        except Exception as exc:
                            result = ToolResult(
                                content=f"Execution failed: {exc}",
                                is_error=True,
                                status="failed",
                            )
                        _record_batch_result(tc, result)
                        if result.is_error and tc.name in COMMAND_OUTPUT_STREAM_TOOL_NAMES:
                            should_cancel_siblings = True
                    async for event in _emit_ordered_ready_results():
                        yield event

                    if should_cancel_siblings:
                        remaining = list(pending.items())
                        for task, _tc in remaining:
                            task.cancel()
                        # Use gather for parallel cancellation — consistent with
                        # the finally block. Sequential await delays sibling cleanup
                        # behind the slowest-to-cancel task (e.g. subprocess teardown).
                        await asyncio.gather(*(task for task, _ in remaining), return_exceptions=True)
                        for task, tc in remaining:
                            _record_batch_result(tc, cancelled_result)
                            pending.pop(task, None)
                        for tc in executable_batch[next_executable_index:]:
                            _record_batch_result(tc, cancelled_result)
                        next_executable_index = len(executable_batch)
                        async for event in _emit_ordered_ready_results():
                            yield event
                        break

                    await _start_ready_tasks()

                if batch_timed_out:
                    remaining = list(pending.items())
                    for task, _tc in remaining:
                        task.cancel()
                    # Use gather for parallel cancellation — consistent with
                    # the finally block. Sequential await delays timeout cleanup
                    # behind the slowest-to-cancel task.
                    await asyncio.gather(*(task for task, _ in remaining), return_exceptions=True)
                    for task, tc in remaining:
                        _record_batch_result(tc, _tool_batch_timeout_result(tc, batch_timeout))
                        pending.pop(task, None)
                    for tc in executable_batch[next_executable_index:]:
                        _record_batch_result(tc, _tool_batch_timeout_result(tc, batch_timeout))
                    next_executable_index = len(executable_batch)
                    async for event in _emit_ordered_ready_results():
                        yield event
            finally:
                # Cancel + await any still-pending tasks. Reached when the
                # generator is closed early (consumer stops iterating / interrupt
                # unwinds through the `async for`). Cancelling without awaiting
                # raises CancelledError at an arbitrary await point and the
                # coroutine's own finally (metadata restore, checkpoint cleanup)
                # may not run before the frame is gone — orphaned subprocess /
                # file handles and "Task was destroyed but it is pending!".
                if pending:
                    for task in pending:
                        task.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)

            while next_emit_index < len(batch):
                tc = batch[next_emit_index]
                results_by_id.setdefault(tc.id, cancelled_result)
                async for event in _emit_ordered_ready_results():
                    yield event
        else:
            for tc in batch:
                yield _tool_started_event(tc, iteration_id=iteration_id)
                await _emit_tool_runtime_span(
                    tc,
                    tool_ctx,
                    event="tool.started",
                    iteration_id=iteration_id,
                    phase="tool",
                    status="running",
                    summary=f"Running {tc.name}",
                )
                duplicate_output_result = duplicate_output_write_guard_result(state, tc)
                if duplicate_output_result is not None:
                    if not _tool_output_was_streamed(tool_ctx, tc.id):
                        for event in tool_output_delta_events(
                            tc,
                            duplicate_output_result,
                            turn_id=turn_id,
                            iteration_id=iteration_id,
                        ):
                            yield event
                    async for event in _finalize_tool_result(
                        tc,
                        duplicate_output_result,
                        ctx=ctx,
                        state=state,
                        status="blocked",
                        iteration_id=iteration_id,
                        turn_id=turn_id,
                        guardrail_controller=guardrail_controller,
                        tool_ctx=tool_ctx,
                        tool_registry=tool_registry,
                    ):
                        yield event
                    continue
                diff = (
                    generate_diff(tc.name, tc.arguments, workspace_root=tool_ctx.workspace_root, tool_ctx=tool_ctx)
                    if tc.name in CHECKPOINT_WRITE_TOOL_NAMES
                    else None
                )
                prefetched = _take_matching_prefetch(prefetched_results, tc)
                if prefetched is not None:
                    result = await _await_prefetched_result(prefetched)
                else:
                    result = await run_tool_with_timeout(tc, tool_registry, tool_ctx, iteration_id=iteration_id)
                if not _tool_output_was_streamed(tool_ctx, tc.id):
                    if tc.name in COMMAND_OUTPUT_STREAM_TOOL_NAMES and result.content and not result.is_error:
                        await _emit_tool_first_output_progress(
                            tc,
                            tool_ctx,
                            iteration_id=iteration_id,
                            detail="Buffered command output available",
                        )
                    for event in tool_output_delta_events(
                        tc,
                        result,
                        turn_id=turn_id,
                        iteration_id=iteration_id,
                    ):
                        yield event
                async for event in _finalize_tool_result(
                    tc,
                    result,
                    ctx=ctx,
                    state=state,
                    diff=None if result.is_error else diff,
                    iteration_id=iteration_id,
                    turn_id=turn_id,
                    guardrail_controller=guardrail_controller,
                    tool_ctx=tool_ctx,
                    tool_registry=tool_registry,
                ):
                    yield event


async def _execute_serial(
    tc: ToolCallEvent,
    *,
    perm: PermissionLevel,
    ctx: ContextBuilder,
    state: AgentState,
    tool_registry: ToolRegistry,
    tool_ctx: ToolExecutionContext,
    approval_handler: Callable | None,
    skill_manager: Any | None,
    iteration_id: str = "",
    guardrail_controller: ToolCallGuardrailController | None = None,
    prefetched: PrefetchedToolExecution | None = None,
) -> AsyncIterator[AgentEvent]:
    diff: dict[str, Any] | None = None
    turn_id = _tool_turn_id(tool_ctx)
    declared_permission = getattr(tool_registry.get_tool(tc.name), "permission", None)
    needs_diff_review = perm == PermissionLevel.DIFF_REVIEW or declared_permission == PermissionLevel.DIFF_REVIEW

    auto_diff_without_handler = (
        needs_diff_review
        and approval_handler is None
        and getattr(tool_ctx.permission, "mode", "") == "auto"
    )
    if perm in (PermissionLevel.CONFIRM, PermissionLevel.DIFF_REVIEW) and not auto_diff_without_handler:
        diff = generate_diff(tc.name, tc.arguments, workspace_root=tool_ctx.workspace_root, tool_ctx=tool_ctx) if needs_diff_review else None
        from backend.hooks import get_hook_manager

        hook_mgr = get_hook_manager()
        permission_allowed_by_hook = False
        if hook_mgr:
            try:
                permission_hook = await hook_mgr.run_permission_request(
                    tc.name,
                    tc.arguments,
                    reason="tool requires user approval",
                    permission_level=perm.value,
                )
                if permission_hook.blocked:
                    message = permission_hook.message or permission_hook.feedback or "permission request blocked by hook"
                    yield AgentEvent.permission_decision(
                        tool_call_id=tc.id,
                        tool_name=tc.name,
                        decision=permission_hook.permission_decision or "deny",
                        permission_level=perm.value,
                        message=message,
                        capability={"allowed": True, "reason": "Capability boundary allows this tool call."},
                        approval_policy=perm.value,
                        matched_rule={"source": "hook", "rule": "permission_request"},
                        scope={"workspace_scope": tool_ctx.permission.workspace_scope},
                        expiry="call",
                    )
                    result = ToolResult(content=f"Permission request blocked by hook: {message}", is_error=True)
                    async for event in _finalize_tool_result(
                        tc,
                        result,
                        ctx=ctx,
                        state=state,
                        diff=diff,
                        iteration_id=iteration_id,
                        turn_id=turn_id,
                        guardrail_controller=guardrail_controller,
                        tool_ctx=tool_ctx,
                    ):
                        yield event
                    return
                if permission_hook.has_permission_decision:
                    yield AgentEvent.permission_decision(
                        tool_call_id=tc.id,
                        tool_name=tc.name,
                        decision=permission_hook.permission_decision,
                        permission_level=perm.value,
                        message=permission_hook.permission_decision_reason or permission_hook.message,
                        capability={"allowed": True, "reason": "Capability boundary allows this tool call."},
                        approval_policy=perm.value,
                        matched_rule={"source": "hook", "rule": "permission_request"},
                        scope={"workspace_scope": tool_ctx.permission.workspace_scope},
                        expiry="call",
                    )
                if permission_hook.permission_decision == "allow":
                    permission_allowed_by_hook = True
            except Exception as exc:
                logger.warning("permission_request hook failed: %s", exc)
        if not permission_allowed_by_hook:
            yield _tool_waiting_approval_event(
                tc,
                iteration_id=iteration_id,
                detail="Awaiting explicit approval before this tool can run",
            )
            await _emit_tool_runtime_span(
                tc,
                tool_ctx,
                event="approval.waiting",
                iteration_id=iteration_id,
                phase="approval",
                status="running",
                summary=f"Waiting for approval: {tc.name}",
                detail="Awaiting explicit approval before this tool can run",
                waiting_on="user",
                blocking_reason="approval_required",
                requires_attention=True,
            )
            yield AgentEvent.approval_request(
                tool_call_id=tc.id,
                tool_name=tc.name,
                args=tc.arguments,
                diff=diff,
                source_agent=str(tool_ctx.metadata.get("run_id") or tool_ctx.metadata.get("agent_role") or "").strip(),
                source_thread=str(tool_ctx.conversation_id or tool_ctx.metadata.get("conversation_id") or tool_ctx.session_id or "").strip(),
                source_tool=tc.name,
            )
            if approval_handler:
                approval = await approval_handler(tc.id)
                if approval.get("action") == "reject":
                    guidance = approval.get("guidance", "user rejected this action")
                    result = ToolResult(content=f"Operation rejected: {guidance}", is_error=True)
                    # Preserve the proposed diff on rejection so the UI can show what
                    # would have been applied (cc shows the rejected edit's diff).
                    async for event in _finalize_tool_result(
                        tc, result, ctx=ctx, state=state, diff=diff,
                        iteration_id=iteration_id, turn_id=turn_id,
                        guardrail_controller=guardrail_controller, tool_ctx=tool_ctx,
                    ):
                        yield event
                    return
                if approval.get("action") == "partial":
                    decisions = approval.get("decisions", {})
                    target = tc.arguments.get("file_path") or tc.arguments.get("path") or ""
                    rejected = [p for p, d in decisions.items() if d == "rejected"]
                    if tc.name == "apply_patch" and rejected:
                        result = ToolResult(content="Operation rejected because one or more patch files were rejected.", is_error=True)
                        async for event in _finalize_tool_result(
                            tc, result, ctx=ctx, state=state, diff=diff,
                            iteration_id=iteration_id, turn_id=turn_id,
                            guardrail_controller=guardrail_controller, tool_ctx=tool_ctx,
                        ):
                            yield event
                        return
                    if target and any(target.endswith(rp) or rp.endswith(target) for rp in rejected):
                        result = ToolResult(content=f"Operation rejected for file: {target}", is_error=True)
                        async for event in _finalize_tool_result(
                            tc, result, ctx=ctx, state=state, diff=diff,
                            iteration_id=iteration_id, turn_id=turn_id,
                            guardrail_controller=guardrail_controller, tool_ctx=tool_ctx,
                        ):
                            yield event
                        return

    if diff is None and tc.name in CHECKPOINT_WRITE_TOOL_NAMES:
        diff = generate_diff(tc.name, tc.arguments, workspace_root=tool_ctx.workspace_root, tool_ctx=tool_ctx)

    yield _tool_started_event(tc, iteration_id=iteration_id)
    await _emit_tool_runtime_span(
        tc,
        tool_ctx,
        event="tool.started",
        iteration_id=iteration_id,
        phase="tool",
        status="running",
        summary=f"Running {tc.name}",
    )

    control_router = ControlToolRouter(
        state=state,
        approval_handler=approval_handler,
        skill_manager=skill_manager,
    )
    for event in control_router.pre_wait_events(tc):
        yield event
    routed = await control_router.execute(tc)
    if routed is not None:
        for event in routed.events:
            yield event
        result = routed.result
    elif prefetched is not None:
        result = await _await_prefetched_result(prefetched)
    else:
        result = await run_tool_with_timeout(tc, tool_registry, tool_ctx, iteration_id=iteration_id)

    async for event in _finalize_tool_result(
        tc,
        result,
        ctx=ctx,
        state=state,
        diff=None if result.is_error else diff,
        iteration_id=iteration_id,
        turn_id=turn_id,
        guardrail_controller=guardrail_controller,
        tool_ctx=tool_ctx,
        tool_registry=tool_registry,
    ):
        yield event


def store_result(
    tc: ToolCallEvent,
    result: ToolResult,
    ctx: ContextBuilder,
    state: AgentState,
    status: str | None = None,
    diff: dict[str, Any] | None = None,
    iteration_id: str = "",
    turn_id: str = "",
    append_to_context: bool = True,
    guardrail_controller: ToolCallGuardrailController | None = None,
    tool_ctx: ToolExecutionContext | None = None,
    tool_registry: ToolRegistry | None = None,
) -> AgentEvent:
    from backend.tools.base import MAX_TOOL_RESULT_CHARS, truncate_tool_result

    final_status = status_for_result(result, status)
    result_kind = result.result_kind or result_kind_for_tool(tc.name)
    limitation = result.limitation or (
        "unsandboxed background command"
        if tc.name == "run_command" and bool(tc.arguments.get("run_in_background"))
        else ""
    )
    started_at = _tool_start_times(state).get(tc.id)
    duration_ms = result.duration_ms
    if duration_ms is None and isinstance(started_at, (int, float)):
        duration_ms = int(max(0.0, time.time() - started_at) * 1000)

    result = guardrail_after_call_result(
        guardrail_controller,
        tc,
        result,
        status=status,
        final_status=final_status,
        append_to_context=append_to_context,
    )
    result = _force_artifact_for_oversized_tool_result(tc, result, tool_ctx)

    # Per-tool result budget. Tools that self-bound and artifact their overflow
    # (read_file, web_fetch, run_command) set max_result_chars=None to opt out of
    # the backstop, so their compact summary isn't truncated a second time.
    cap: int | None = MAX_TOOL_RESULT_CHARS
    if tool_registry is not None:
        tool_obj = tool_registry.get_tool(tc.name)
        if tool_obj is not None:
            cap = getattr(tool_obj, "max_result_chars", MAX_TOOL_RESULT_CHARS)
    if cap is None:
        truncated = result
    else:
        truncated = replace(result, content=truncate_tool_result(result.content, cap))
    display_summary = display_summary_for_result(tc, truncated, status=final_status, diff=diff)
    issue = classify_tool_issue(tc, truncated, final_status)
    issue_projection = issue.projection if issue else ""
    tool_projection = DEFAULT_PROJECTION_REGISTRY.project_tool_call(tc.name, tc.arguments or {})
    display_scope = truncated.display_scope or tool_projection.display_scope or "activity"
    panel_hint = "" if display_scope == "silent" else _panel_hint_for_tool_result(tc.name, result_kind, diff)
    requires_attention = _requires_attention_for_tool_result(
        final_status=final_status,
        projection=issue_projection,
        result_kind=result_kind,
        diff=diff,
    )
    side_effect_kind = _tool_side_effect_kind(tc.name, tool_registry, tc.arguments)
    idempotent = _tool_is_idempotent(tc.name, tool_registry, tc.arguments) if tool_registry is not None else False
    idempotency_key = ""
    if tool_registry is not None:
        tool_obj = tool_registry.get_tool(tc.name)
        get_key = getattr(tool_obj, "idempotency_key", None)
        if callable(get_key):
            try:
                idempotency_key = str(get_key(tc.arguments) or "")
            except Exception:
                idempotency_key = ""
    truncated = replace(
        truncated,
        status=final_status,
        duration_ms=duration_ms,
        display_summary=display_summary,
        result_kind=result_kind,
        limitation=limitation or truncated.limitation,
        display_scope=display_scope,
    )
    if truncated.status == "timeout" and truncated.limitation == "non-critical timeout":
        state.add_loop_guidance(
            f"Optional tool {tc.name} timed out. Do not retry it this turn; continue with the user-facing answer."
        )
    if issue and issue.model_observation:
        state.add_loop_guidance(issue.model_observation)
    if append_to_context:
        ctx.append_tool_result(tc.id, tc.name, truncated)
    elif truncated.content:
        state.add_loop_guidance(truncated.content)
    state.record_tool_call(
        tc.name,
        tc.arguments,
        truncated.to_context_string(),
        artifact_id=truncated.artifact_id,
        is_error=truncated.is_error,
        mutates=_tool_mutates(tc.name, tool_registry),
        status=final_status,
        source_url=truncated.source_url,
        extraction_status=truncated.extraction_status,
        content_preview=truncated.content_preview,
        evidence_type=truncated.evidence_type,
        provider=truncated.provider,
        provider_error_type=truncated.provider_error_type,
        error_kind=issue.error_kind if issue else None,
        user_summary=issue.user_summary if issue else None,
        developer_detail=issue.developer_detail if issue else None,
        projection=issue.projection if issue else None,
        turn_id=turn_id or None,
        iteration_id=iteration_id or f"iter:{max(1, state.iterations)}",
    )
    if truncated.evidence_type:
        state.evidence_records.append(
            EvidenceRecord(
                source_url=truncated.source_url or "",
                source_name=truncated.provider or "",
                retrieved_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                evidence_type=truncated.evidence_type,
                confidence=0.7 if truncated.evidence_type == "fetched" else 0.35,
                tool_call_id=tc.id,
                tool_name=tc.name,
            )
        )
    # Build the unified ToolOutcome so downstream consumers (Inspector,
    # context ledger, event aggregation) can read from a single typed record
    # instead of probing individual event.data fields.
    _outcome_status_map = {
        "success": ToolOutcomeStatus.COMPLETED,
        "completed": ToolOutcomeStatus.COMPLETED,
        "failed": ToolOutcomeStatus.FAILED,
        "blocked": ToolOutcomeStatus.BLOCKED,
        "timeout": ToolOutcomeStatus.TIMEOUT,
        "partial": ToolOutcomeStatus.TIMEOUT,
    }
    _outcome = ToolOutcome(
        call_id=tc.id,
        tool_name=tc.name,
        status=_outcome_status_map.get(final_status, ToolOutcomeStatus.COMPLETED),
        content=truncated.content,
        error=truncated.content if final_status in {"failed", "blocked", "timeout"} else "",
        result_kind=result_kind,
        activity_kind=tool_projection.activity_kind,
        panel_hint=panel_hint or "inspector",
        side_effect_kind=side_effect_kind,
        idempotent=idempotent,
        started_at=int(started_at * 1000) if isinstance(started_at, (int, float)) else 0,
        completed_at=int(time.time() * 1000),
        artifact_id=truncated.artifact_id,
        artifact_preview=truncated.artifact_preview,
        display_summary=display_summary,
        source_url=truncated.source_url,
        evidence_type=truncated.evidence_type,
        provider=truncated.provider,
        provider_error_type=truncated.provider_error_type,
        duration_ms=duration_ms,
        requires_attention=requires_attention,
    )
    return AgentEvent.tool_result(
        id=tc.id,
        summary=truncated.content,
        artifact_id=truncated.artifact_id,
        is_error=truncated.is_error,
        diff=diff,
        source_url=truncated.source_url,
        extraction_status=truncated.extraction_status,
        content_preview=truncated.content_preview,
        evidence_type=truncated.evidence_type,
        status=final_status,
        duration_ms=duration_ms,
        display_summary=display_summary,
        result_kind=result_kind,
        limitation=limitation or truncated.limitation or "",
        provider=truncated.provider or "",
        provider_error_type=truncated.provider_error_type or "",
        error_info=issue.to_dict() if issue else None,
        error_kind=issue.error_kind if issue else "",
        user_summary=issue.user_summary if issue else "",
        developer_detail=issue.developer_detail if issue else "",
        recoverable=issue.recoverable if issue else True,
        projection=issue_projection,
        turn_id=turn_id,
        group_id=iteration_id,
        step_id=tc.id,
        iteration_id=iteration_id,
        phase="tool",
        display_scope=display_scope,
        panel_hint=panel_hint,
        requires_attention=requires_attention,
        side_effect_kind=side_effect_kind,
        idempotent=idempotent,
        idempotency_key=idempotency_key,
        outcome=_outcome.to_dict(),
    )


def tool_result_progress_event(
    tc: ToolCallEvent,
    result: ToolResult,
    *,
    final_status: str,
    display_summary: str,
    iteration_id: str = "",
    diff: dict[str, Any] | None = None,
    issue_projection: str = "",
) -> AgentEvent:
    status = "completed"
    if final_status in {"failed", "blocked", "timeout"}:
        status = "failed"
    requires_attention = final_status in {"failed", "blocked", "timeout"} and issue_projection not in {"silent", "status", "warning"}
    panel_hint = _panel_hint_for_tool_result(tc.name, result.result_kind or result_kind_for_tool(tc.name), diff)
    message = display_summary or (f"Completed {tc.name}" if status == "completed" else f"Failed {tc.name}")
    detail = result.limitation or result.content or ""
    return _tool_progress_event(
        tc,
        message,
        phase="tool",
        status=status,
        visibility="compact",
        detail=detail,
        iteration_id=iteration_id,
        display_scope=result.display_scope or "activity",
        panel_hint=panel_hint,
        requires_attention=requires_attention,
    )


def store_result_events(
    tc: ToolCallEvent,
    result: ToolResult,
    ctx: ContextBuilder,
    state: AgentState,
    *,
    status: str | None = None,
    diff: dict[str, Any] | None = None,
    iteration_id: str = "",
    turn_id: str = "",
    append_to_context: bool = True,
    guardrail_controller: ToolCallGuardrailController | None = None,
    tool_ctx: ToolExecutionContext | None = None,
    tool_registry: ToolRegistry | None = None,
) -> list[AgentEvent]:
    event = store_result(
        tc,
        result,
        ctx,
        state,
        status=status,
        diff=diff,
        iteration_id=iteration_id,
        turn_id=turn_id,
        append_to_context=append_to_context,
        guardrail_controller=guardrail_controller,
        tool_ctx=tool_ctx,
        tool_registry=tool_registry,
    )
    progress_event = tool_result_progress_event(
        tc,
        result,
        final_status=str(event.data.get("status") or "success"),
        display_summary=str(event.data.get("display_summary") or event.data.get("summary") or ""),
        iteration_id=iteration_id,
        diff=diff,
        issue_projection=str(event.data.get("projection") or ""),
    )
    return [progress_event, event]


def _resolve_workspace_path_for_diff(file_path: str, workspace_root: Path | str | None) -> Path:
    path = Path(str(file_path))
    if path.is_absolute():
        return path
    if workspace_root:
        return Path(workspace_root).resolve() / path
    return path


def generate_diff(
    tool_name: str,
    args: dict[str, Any],
    *,
    workspace_root: Path | str | None = None,
    tool_ctx: ToolExecutionContext | None = None,
) -> dict[str, Any] | None:
    _meta = tool_ctx.metadata if (tool_ctx is not None and isinstance(tool_ctx.metadata, dict)) else {}
    _read_time_hashes = _meta.get("_read_file_hashes")
    if not isinstance(_read_time_hashes, dict):
        _read_time_hashes = None
    if tool_name == "write_file":
        file_path = args.get("file_path", "")
        content = args.get("content", "")
        if file_path and content:
            resolved_path = _resolve_workspace_path_for_diff(str(file_path), workspace_root)
            inject_expected_hash(args, str(resolved_path), read_time_hashes=_read_time_hashes)
            return generate_file_diff_payload(str(resolved_path), content)
    elif tool_name == "edit_file":
        file_path = args.get("file_path", "")
        old_string = args.get("old_string", "")
        new_string = args.get("new_string", "")
        if file_path and old_string:
            resolved_path = _resolve_workspace_path_for_diff(str(file_path), workspace_root)
            inject_expected_hash(args, str(resolved_path), read_time_hashes=_read_time_hashes)
            return generate_edit_diff_payload(str(resolved_path), old_string, new_string)
    elif tool_name == "apply_patch":
        from backend.tools.apply_patch import build_apply_patch_diff_payload

        patch_text = args.get("patch")
        if isinstance(patch_text, str):
            expected_hashes: dict[str, str] = {}
            payload = build_apply_patch_diff_payload(
                patch_text,
                tool_ctx,
                expected_hashes=expected_hashes,
                read_time_hashes=_read_time_hashes,
            )
            if payload is not None:
                args["_expected_hashes"] = expected_hashes
            return payload
    return None


def inject_expected_hash(
    args: dict[str, Any],
    file_path: str,
    *,
    read_time_hashes: dict[str, str] | None = None,
) -> None:
    if str(args.get("expected_hash") or "").strip():
        return
    path = Path(str(file_path))
    path_key = str(path)
    # Prefer the READ-TIME content hash (recorded by read_file) so the
    # "file changed since read" guard actually spans the read->write window,
    # not just the sub-second diff->write window. Falls back to the current
    # on-disk hash only when the file was never read in this conversation,
    # preserving existing behavior there.
    if isinstance(read_time_hashes, dict) and read_time_hashes.get(path_key):
        args["expected_hash"] = read_time_hashes[path_key]
        return
    if not path.exists():
        args["expected_hash"] = ""
        return
    try:
        content = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        args["expected_hash"] = "__minicode_unreadable_existing_file__"
        return
    args["expected_hash"] = hashlib.sha256(content.encode("utf-8")).hexdigest()
