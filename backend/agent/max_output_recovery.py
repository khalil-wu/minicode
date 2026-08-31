"""Terminal handling for provider output-length truncation."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from backend.agent.message import AgentEvent
from backend.agent.provider_protocol import usage_terminal_projection
from backend.agent.terminal_projection import TurnTerminalProjection
from backend.agent.response_utils import append_assistant_history
from backend.agent.turn_kernel import _set_terminal_reason


RecoveryAction = Literal["retry", "terminate"]
_MAX_OUTPUT_RECOVERY_LIMIT = 3
_MAX_OUTPUT_NO_PROGRESS_LIMIT = 2
_MAX_OUTPUT_RECOVERY_PROGRESS_ID = "max_output_recovery"
_CONTINUATION_PROMPT = (
    "Output token limit hit. Resume directly - no apology, no recap of what you were doing. "
    "Pick up mid-thought if that is where the cut happened. Break remaining work into smaller pieces."
)
_MAX_OUTPUT_ERROR = (
    "The provider stopped because the output limit was reached and continuation "
    "recovery was exhausted."
)
_CONTEXT_WINDOW_ERROR = (
    "The provider reached the model context window and emergency compaction "
    "could not produce a resumable continuation."
)


def _context_window_failure_error() -> str:
    """Explicit failure text for a gateway that rejects the configured window.

    MiniCode does not rewrite the user's configured token budget on the fly:
    a provider/gateway whose real limit is below the configured budget is a
    configuration error and must be surfaced for the user to fix.
    """

    return (
        "Provider 拒绝了当前上下文窗口，且紧急压缩无法产生可续跑的延续。"
        "MiniCode 不会自动改写你配置的 token 预算；"
        "请把该 provider/model 的上下文预算调整到网关实际允许的值后重试。"
    )


def _request_output_cap(provider_raw_done: dict[str, Any] | None) -> tuple[str, int]:
    raw_done = provider_raw_done if isinstance(provider_raw_done, dict) else {}
    request_summary = raw_done.get("request_summary")
    params = (
        request_summary.get("request_params")
        if isinstance(request_summary, dict)
        else {}
    )
    if not isinstance(params, dict):
        return "", 0
    for field in ("max_tokens", "max_completion_tokens", "max_output_tokens"):
        value = params.get(field)
        if isinstance(value, bool):
            continue
        try:
            cap = int(value)
        except (TypeError, ValueError):
            continue
        if cap > 0:
            return field, cap
    return "", 0


def _max_output_no_progress_error(provider_raw_done: dict[str, Any] | None) -> str:
    field, cap = _request_output_cap(provider_raw_done)
    cap_detail = (
        f"MiniCode 已发送 {field}={cap}。"
        if field and cap > 0
        else "MiniCode 未能从安全请求摘要中确认显式输出上限。"
    )
    return (
        "Provider 连续报告输出上限，但没有返回可恢复文本。"
        f"{cap_detail}"
        "请检查兼容网关或模型的输出限制。"
    )


def _continuation_provider_items(
    provider_items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Drop executable calls while retaining opaque continuation state."""

    replay_items: list[dict[str, Any]] = []
    for item in provider_items:
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type") or "")
        if item_type in {"function_call", "tool_call", "tool_use"}:
            continue
        if item_type == "anthropic_message":
            content = item.get("content")
            if not isinstance(content, list):
                continue
            safe_content = [
                dict(block)
                for block in content
                if isinstance(block, dict)
                and str(block.get("type") or "")
                not in {"function_call", "tool_call", "tool_use"}
            ]
            if safe_content:
                replay_items.append(
                    {"type": "anthropic_message", "content": safe_content}
                )
            continue
        replay_items.append(dict(item))
    return replay_items


@dataclass(frozen=True, slots=True)
class MaxOutputRecoveryResult:
    action: RecoveryAction
    events: tuple[AgentEvent | TurnTerminalProjection, ...]


async def recover_max_output(
    *,
    state: Any,
    stream_text: Any,
    tool_tracker: Any,
    context_builder: Any,
    budget_runtime: Any,
    provider_items: list[dict[str, Any]],
    turn_usage: Any,
    finish_reason: str,
    scrub_text: Any,
    run_stop_failure_hook: Callable[..., Awaitable[None]],
    provider_raw_done: dict[str, Any] | None = None,
) -> MaxOutputRecoveryResult:
    """Continue a truncated answer with CC's bounded multi-turn recovery."""

    tool_tracker.cancel_remaining()
    normalized_reason = str(finish_reason or "").strip().lower()
    context_window_exceeded = normalized_reason == "model_context_window_exceeded"
    partial_text = stream_text.accepted_answer_text(scrub_text)
    previous_partial = str(getattr(state, "max_output_last_partial_text", "") or "")
    new_partial_text = partial_text
    if partial_text.strip() and previous_partial:
        if partial_text == previous_partial:
            new_partial_text = ""
        elif partial_text.startswith(previous_partial):
            new_partial_text = partial_text[len(previous_partial) :]
    no_progress_count = int(getattr(state, "max_output_no_progress_count", 0) or 0)
    if new_partial_text.strip():
        no_progress_count = 0
    else:
        no_progress_count += 1
    state.max_output_no_progress_count = no_progress_count
    if partial_text.strip():
        state.max_output_last_partial_text = partial_text
    events: list[AgentEvent | TurnTerminalProjection] = []
    if new_partial_text.strip():
        state.max_output_partial_text = (
            str(getattr(state, "max_output_partial_text", "") or "") + new_partial_text
        )
        started = stream_text.start_agent_message()
        if started is not None:
            events.append(started)
        completed = stream_text.complete_active_agent_message(
            new_partial_text,
            source="partial",
            status="partial",
            finish_reason=finish_reason,
        )
        if completed is not None:
            events.append(completed)

    no_progress_exhausted = (
        not context_window_exceeded
        and no_progress_count >= _MAX_OUTPUT_NO_PROGRESS_LIMIT
    )
    recovery_count = int(getattr(state, "max_output_recovery_count", 0) or 0)
    if not no_progress_exhausted and recovery_count < _MAX_OUTPUT_RECOVERY_LIMIT:
        boundary = budget_runtime.consume_retry("max_output_tokens_recovery")
        if boundary is None:
            context_compacted = False
            if context_window_exceeded:
                if not bool(getattr(state, "reactive_compaction_attempted", False)):
                    state.reactive_compaction_attempted = True
                    full_compact = getattr(context_builder, "full_compact", None)
                    if callable(full_compact):
                        try:
                            summary = await full_compact(restore_state=state)
                        except Exception:
                            summary = ""
                        context_compacted = bool(summary)
                if not context_compacted:
                    recovery_count = _MAX_OUTPUT_RECOVERY_LIMIT
                else:
                    state.mark_transition(
                        "recovered_emergency_compact",
                        finish_reason=normalized_reason,
                    )
            if recovery_count >= _MAX_OUTPUT_RECOVERY_LIMIT:
                boundary = object()
            else:
                replay_items = _continuation_provider_items(provider_items)
                if new_partial_text.strip() or replay_items:
                    append_assistant_history(
                        context_builder,
                        new_partial_text,
                        phase="final_answer",
                        provider_items=replay_items,
                    )
                context_builder.append_user(_CONTINUATION_PROMPT)
                state.max_output_recovery_count = recovery_count + 1
                state.mark_transition(
                    "max_output_tokens_recovery",
                    attempt=state.max_output_recovery_count,
                    context_compacted=context_compacted,
                )
                stream_text.reset_for_retry()
                events.append(
                    AgentEvent.progress(
                        "正在恢复被截断的输出",
                        id=_MAX_OUTPUT_RECOVERY_PROGRESS_ID,
                        stage="status",
                        status="running",
                        phase="recover",
                        ephemeral=True,
                        visibility="compact",
                    )
                )
                return MaxOutputRecoveryResult("retry", tuple(events))

    accumulated_partial = str(getattr(state, "max_output_partial_text", "") or "")
    if accumulated_partial.strip():
        state.reply = accumulated_partial
    terminal_error = (
        _max_output_no_progress_error(provider_raw_done)
        if no_progress_exhausted
        else (
            _context_window_failure_error()
            if context_window_exceeded
            else _MAX_OUTPUT_ERROR
        )
    )
    await run_stop_failure_hook(
        "model_context_window_exceeded"
        if context_window_exceeded
        else "max_output_tokens",
        error_details=terminal_error,
        last_assistant_message=accumulated_partial,
    )
    if no_progress_exhausted:
        events.append(
            AgentEvent.progress(
                terminal_error,
                id=_MAX_OUTPUT_RECOVERY_PROGRESS_ID,
                stage="status",
                status="failed",
                phase="recover",
                ephemeral=True,
                visibility="compact",
            )
        )
    events.append(
        AgentEvent.error(
            message=terminal_error,
            recoverable=True,
            error_type=("context_window" if context_window_exceeded else "max_output"),
        )
    )
    status = "partial" if accumulated_partial.strip() else "failed"
    terminal_reason = "context_window" if context_window_exceeded else "max_output"
    _set_terminal_reason(state, terminal_reason, status=status)
    events.append(
        usage_terminal_projection(turn_usage, status=status, reason=terminal_reason)
    )
    return MaxOutputRecoveryResult("terminate", tuple(events))
