"""Provider request metadata, stream traces, usage and cache diagnostics."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from backend.agent.context import ContextBuilder
from backend.agent.message import AgentEvent
from backend.agent.state import AgentState
from backend.llm.base import LLMAdapter, UsageInfo
from backend.llm.capabilities import capabilities_for_adapter


def set_context_stateful_history_preference(ctx: ContextBuilder, enabled: bool) -> None:
    setter = getattr(ctx, "set_prefer_stateful_history", None)
    if callable(setter):
        setter(bool(enabled))


def provider_stateful_history_preferred(llm: LLMAdapter) -> bool:
    try:
        return bool(capabilities_for_adapter(llm).stateful_continuation)
    except Exception:
        return False


def provider_stateful_history_effective(provider_raw: dict[str, Any]) -> bool | None:
    summary = provider_raw.get("stateful_continuation") if isinstance(provider_raw, dict) else None
    if not isinstance(summary, dict):
        return None
    if summary.get("enabled") is False:
        return False
    if summary.get("stored_response_id_hash") or summary.get("used") is True:
        return True
    if summary.get("configured") is True and summary.get("enabled") is True:
        return False
    return None


def usage_done_event(
    usage: UsageInfo,
    provider_raw: dict[str, Any] | None = None,
    *,
    status: str = "completed",
    reason: str = "",
) -> AgentEvent:
    return AgentEvent.done(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_creation_input_tokens=usage.cache_creation_input_tokens,
        cache_read_input_tokens=usage.cache_read_input_tokens,
        cache_deleted_input_tokens=usage.cache_deleted_input_tokens,
        reasoning_output_tokens=usage.reasoning_output_tokens,
        input_includes_cache_read=usage.input_includes_cache_read,
        provider_raw=provider_raw,
        status=status,
        reason=reason,
    )


def add_usage(left: UsageInfo, right: UsageInfo | None) -> UsageInfo:
    """Accumulate into the turn-owned mutable usage bucket.

    ``LLMAdapter.bind_turn_usage`` keeps a ContextVar reference to ``left`` so
    non-stream side calls can charge the same turn. Replacing that object here
    would split accounting after the first provider response: stream usage
    would move to a new object while compaction/recovery kept mutating the
    stale bound bucket.
    """
    if right is None:
        return left
    left.input_tokens += int(getattr(right, "input_tokens", 0) or 0)
    left.output_tokens += int(getattr(right, "output_tokens", 0) or 0)
    left.cache_creation_input_tokens += int(
        getattr(right, "cache_creation_input_tokens", 0) or 0
    )
    left.cache_read_input_tokens += int(
        getattr(right, "cache_read_input_tokens", 0) or 0
    )
    left.cache_deleted_input_tokens = max(
        left.cache_deleted_input_tokens,
        int(getattr(right, "cache_deleted_input_tokens", 0) or 0),
    )
    left.reasoning_output_tokens += int(
        getattr(right, "reasoning_output_tokens", 0) or 0
    )
    if not bool(getattr(right, "input_includes_cache_read", True)):
        left.input_includes_cache_read = False
    return left


def build_llm_request_metadata(
    *,
    metadata: dict[str, Any],
    session_id: str,
    task_id: str,
    workspace_root: Path | None,
    run_id: str,
    conversation_id: str,
) -> dict[str, Any]:
    explicit = metadata.get("llm_request_metadata")
    remaining: dict[str, Any] = dict(explicit) if isinstance(explicit, dict) else {}
    request: dict[str, Any] = {}

    def pop_explicit(key: str, fallback: Any = "") -> Any:
        value = remaining.pop(key, None)
        if value is None or (isinstance(value, str) and not value.strip()):
            return fallback
        return value

    source = pop_explicit("minicode_source", str(metadata.get("minicode_source") or "desktop"))
    if source:
        request["minicode_source"] = source
    if session_id or remaining.get("minicode_session_id"):
        request["minicode_session_id"] = pop_explicit("minicode_session_id", session_id)
    app_session_id = pop_explicit(
        "minicode_app_session_id",
        request.get("minicode_session_id", session_id),
    )
    if app_session_id:
        request["minicode_app_session_id"] = app_session_id
    if task_id or remaining.get("minicode_task_id"):
        request["minicode_task_id"] = pop_explicit("minicode_task_id", task_id)
    cwd = str(workspace_root) if workspace_root is not None else metadata.get("cwd")
    if cwd or remaining.get("cwd"):
        request["cwd"] = pop_explicit("cwd", cwd)
    if conversation_id or remaining.get("conversation_id"):
        request["conversation_id"] = pop_explicit("conversation_id", conversation_id)
    if run_id or remaining.get("run_id"):
        request["run_id"] = pop_explicit("run_id", run_id)
    assistant_message_id = str(metadata.get("assistant_message_id") or "").strip()
    if assistant_message_id or remaining.get("turn_id"):
        request["turn_id"] = pop_explicit("turn_id", assistant_message_id)
    if assistant_message_id or remaining.get("assistant_message_id"):
        request["assistant_message_id"] = pop_explicit("assistant_message_id", assistant_message_id)
    for key, value in remaining.items():
        request.setdefault(key, value)
    return request


def annotate_request_metadata_with_prompt_cache_fork(
    request_metadata: dict[str, Any],
    fork: dict[str, Any],
) -> None:
    if not isinstance(fork, dict) or not fork:
        return
    prefix_shadow = fork.get("prefix_shadow")
    if not isinstance(prefix_shadow, dict):
        prefix_shadow = {}
    schema_shadow = fork.get("schema_shadow")
    if not isinstance(schema_shadow, dict):
        schema_shadow = {}
    scalar_fields = {
        "prompt_cache_fork_status": fork.get("status"),
        "prompt_cache_fork_stable_prefix": fork.get("stable_prefix"),
        "prompt_cache_parent_stable_hash": prefix_shadow.get("parent_stable_system_hash"),
        "prompt_cache_child_stable_hash": prefix_shadow.get("child_stable_system_hash"),
        "prompt_cache_parent_tools_hash": schema_shadow.get("parent_tools_hash"),
        "prompt_cache_child_tools_hash": schema_shadow.get("child_tools_hash"),
    }
    for key, value in scalar_fields.items():
        text = str(value or "").strip()
        if text:
            request_metadata[key] = text


def prompt_cache_tracking_source(*, run_record: Any, session_id: str, task_id: str) -> str:
    role = str(getattr(run_record, "role", "") or "main")
    conversation_id = str(getattr(run_record, "conversation_id", "") or "").strip()
    if role == "main":
        return f"main:{conversation_id or session_id or 'default'}"
    return f"{role}:{task_id or getattr(run_record, 'run_id', '') or session_id or 'default'}"


def merge_prompt_cache_safe_request_summary(
    request_summary: Any,
    prompt_cache_safe_params: dict[str, Any] | None,
) -> dict[str, Any]:
    summary = dict(request_summary) if isinstance(request_summary, dict) else {}
    safe = prompt_cache_safe_params if isinstance(prompt_cache_safe_params, dict) else {}
    if not safe:
        return summary
    field_defaults = {
        "instructions_hash": safe.get("stable_system_hash"),
        "instructions_full_hash": safe.get("full_system_hash"),
        "tools_hash": safe.get("tools_hash"),
        "tool_names": safe.get("tool_names"),
        "tools_chars": safe.get("tools_chars"),
        "largest_tools": safe.get("largest_tools"),
        "metadata_keys": safe.get("metadata_keys"),
    }
    for key, value in field_defaults.items():
        if key not in summary and value not in (None, "", []):
            summary[key] = value
    section_summary = safe.get("prompt_section_summary")
    if isinstance(section_summary, dict) and section_summary:
        summary.setdefault("prompt_section_summary", dict(section_summary))
    if "message_count" not in summary:
        try:
            summary["message_count"] = int(safe.get("message_count") or 0)
        except (TypeError, ValueError):
            pass
    return summary


def provider_trace_payload(
    *,
    provider_raw: dict[str, Any],
    usage: UsageInfo,
    finish_reason: str,
    iteration_id: str,
    call_index: int,
    loop_metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    raw = dict(provider_raw or {})
    request_summary = merge_prompt_cache_safe_request_summary(
        raw.get("request_summary") if isinstance(raw.get("request_summary"), dict) else {},
        raw.get("prompt_cache_safe_params")
        if isinstance(raw.get("prompt_cache_safe_params"), dict)
        else {},
    )
    return {
        "kind": "provider_trace",
        "provider": raw.get("provider") or "",
        "model": raw.get("model") or "",
        "finish_reason": finish_reason or raw.get("finish_reason") or "",
        "event_type": raw.get("event_type") or "",
        "usage": {
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cache_creation_input_tokens": usage.cache_creation_input_tokens,
            "cache_read_input_tokens": usage.cache_read_input_tokens,
            "cache_deleted_input_tokens": usage.cache_deleted_input_tokens,
            "reasoning_output_tokens": usage.reasoning_output_tokens,
            "input_includes_cache_read": usage.input_includes_cache_read,
        },
        "raw_usage": raw.get("usage") if isinstance(raw.get("usage"), dict) else {},
        "output_items": raw.get("output_items") if isinstance(raw.get("output_items"), list) else [],
        "provider_timeline": raw.get("provider_timeline") if isinstance(raw.get("provider_timeline"), list) else [],
        "request_summary": request_summary,
        "prompt_cache_diagnostic": raw.get("prompt_cache_diagnostic") if isinstance(raw.get("prompt_cache_diagnostic"), dict) else {},
        "safety": raw.get("safety") if isinstance(raw.get("safety"), dict) else {"redacted_prompt": True},
        "stateful_continuation": raw.get("stateful_continuation") if isinstance(raw.get("stateful_continuation"), dict) else {},
        "loop_metrics": raw.get("loop_metrics") if isinstance(raw.get("loop_metrics"), dict) else dict(loop_metrics or {}),
        "iteration_id": iteration_id,
        "call_index": call_index,
    }


def loop_metrics_payload(
    *,
    turn_started_at: int,
    state: AgentState,
    provider_call_count: int,
    iteration_limit: int,
    iteration_hard_limit: int,
    tool_batch_count: int,
    turn_start_tool_call_count: int,
    pending_tool_call_count: int = 0,
) -> dict[str, Any]:
    """Build stable loop metrics."""
    completed = max(0, len(state.tool_calls) - max(0, int(turn_start_tool_call_count or 0)))
    pending = max(0, int(pending_tool_call_count or 0))
    now = int(time.time() * 1000)
    return {
        "provider_call_count": max(0, int(provider_call_count or 0)),
        "iteration": max(0, int(state.iterations or 0)),
        "iteration_limit": max(0, int(iteration_limit or 0)),
        "iteration_hard_limit": max(0, int(iteration_hard_limit or 0)),
        "tool_batch_count": max(0, int(tool_batch_count or 0)),
        "tool_call_count": completed + pending,
        "completed_tool_call_count": completed,
        "pending_tool_call_count": pending,
        "elapsed_ms": max(0, now - int(turn_started_at or now)),
    }
