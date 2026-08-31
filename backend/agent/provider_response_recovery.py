"""Post-stream terminal handling before final answer or tool execution."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from functools import partial
from typing import Any, Literal

from backend.agent.loop_preflight import run_stop_failure_hook
from backend.agent.loop_runtime_helpers import is_max_output_finish_reason
from backend.agent.loop_process_events import model_process_text_event
from backend.agent.max_output_recovery import recover_max_output
from backend.agent.message import AgentEvent
from backend.agent.provider_protocol import usage_terminal_projection
from backend.agent.response_utils import append_assistant_history
from backend.agent.turn_kernel import _set_terminal_reason
from backend.agent.terminal_projection import TurnTerminalProjection
from backend.agent.tool_events import abandoned_tool_announcement_events


PostStreamAction = Literal["proceed", "retry", "terminate"]


@dataclass(frozen=True, slots=True)
class PostStreamRecoveryResult:
    action: PostStreamAction
    tool_batch_count: int
    degraded_reason: str


_FENCED_CODE_RE = re.compile(r"(?s)(?:```|~~~).*?(?:```|~~~)")
_INLINE_CODE_RE = re.compile(r"`[^`\r\n]*`")
_TEXTUAL_INVOKE_RE = re.compile(
    r"<\s*invoke\b[^>]*\bname\s*=\s*['\"]?([A-Za-z_][A-Za-z0-9_.:-]*)['\"]?[^>]*>",
    re.IGNORECASE,
)
_TEXTUAL_PARAMETER_RE = re.compile(
    r"<\s*parameter\b[^>]*\bname\s*=",
    re.IGNORECASE,
)
_PROVIDER_CONTINUATION_RECOVERY_LIMIT = 8
_PROVIDER_CONTINUATION_ERROR = (
    "The provider repeatedly stopped for a managed continuation and the "
    "bounded recovery limit was exhausted."
)


def textual_tool_call_imitation(
    text: str,
    *,
    exposed_tool_names: set[str],
) -> str:
    """Return an exposed tool name imitated with XML instead of the wire protocol.

    Provider-native structured tool items are the only executable tool-call
    transport. This is a validator, not an XML compatibility parser:
    fenced/inline examples remain ordinary answer text, while an
    executable-looking ``<invoke name=...><parameter ...>`` sequence fails
    visibly.
    """

    if not text or "<" not in text or not exposed_tool_names:
        return ""
    visible = _INLINE_CODE_RE.sub("", _FENCED_CODE_RE.sub("", text))
    for match in _TEXTUAL_INVOKE_RE.finditer(visible):
        tool_name = match.group(1)
        if tool_name not in exposed_tool_names:
            continue
        # Requiring a parameter tag near the invocation avoids treating prose
        # that merely names the XML form as an attempted execution.  The window
        # also catches malformed/missing closing tags such as the observed
        # provider output while keeping the check bounded.
        tail = visible[match.end() : match.end() + 4096]
        if _TEXTUAL_PARAMETER_RE.search(tail):
            return tool_name
    return ""


async def recover_provider_response(
    *,
    state: Any,
    stream_state: Any,
    stream_text: Any,
    tool_tracker: Any,
    context_builder: Any,
    budget_runtime: Any,
    turn_usage: Any,
    finish_reason: str,
    scrub_text: Any,
    tool_batch_count: int,
    degraded_reason: str,
    exposed_tool_names: set[str] | None = None,
) -> AsyncIterator[AgentEvent | TurnTerminalProjection | PostStreamRecoveryResult]:
    pending_tool_calls = stream_state.tool_calls
    normalized_finish_reason = str(finish_reason or "").strip().lower()

    # A truncated or cut stream can leave an announced tool block with no
    # arguments. Nothing else will ever close it, so settle it here before any
    # branch decides to retry, terminate or proceed.
    for abandoned in abandoned_tool_announcement_events(
        stream_state,
        iteration_id=str(getattr(stream_text, "iteration_id", "") or ""),
    ):
        yield abandoned

    if normalized_finish_reason in {"pause_turn", "compaction"}:
        # Anthropic requires the complete assistant content to be submitted
        # back unchanged. A visible-text-only continuation would lose hosted
        # server tool/result blocks and thinking signatures, so fail closed if
        # the adapter did not retain the native assistant item.
        replay_items = [
            dict(item)
            for item in stream_state.response_items
            if isinstance(item, dict)
            and str(item.get("type") or "") == "anthropic_message"
            and isinstance(item.get("content"), list)
        ]
        if not replay_items:
            tool_tracker.cancel_remaining()
            yield AgentEvent.error(
                message=(
                    f"The provider returned stop_reason={normalized_finish_reason} "
                    "without a replayable "
                    "assistant content item; the turn was not continued."
                ),
                recoverable=True,
                error_type="provider_protocol",
                error_code=(f"{normalized_finish_reason}_missing_provider_state"),
            )
            _set_terminal_reason(state, "provider_protocol", status="failed")
            yield usage_terminal_projection(
                turn_usage,
                status="failed",
                reason=f"{normalized_finish_reason}_missing_provider_state",
            )
            yield PostStreamRecoveryResult(
                "terminate", tool_batch_count, degraded_reason
            )
            return

        tool_tracker.cancel_remaining()
        recovery_count = int(
            getattr(state, "provider_continuation_recovery_count", 0) or 0
        )
        if recovery_count < _PROVIDER_CONTINUATION_RECOVERY_LIMIT:
            retry_reason = f"provider_{normalized_finish_reason}"
            boundary = budget_runtime.consume_retry(retry_reason)
            if boundary is None:
                intermediate_text = stream_text.pending_recovery_text(scrub_text)
                if intermediate_text.strip():
                    completed = stream_text.complete_active_agent_message(
                        intermediate_text,
                        source="commentary",
                        status="completed",
                        finish_reason=normalized_finish_reason,
                    )
                    if completed is not None:
                        yield completed
                    else:
                        process_event = model_process_text_event(
                            intermediate_text,
                            [],
                            iteration_id=stream_text.iteration_id,
                            source="provider_continuation",
                            status="completed",
                        )
                        if process_event is not None:
                            yield process_event
                append_assistant_history(
                    context_builder,
                    intermediate_text,
                    phase="commentary",
                    provider_items=replay_items,
                )
                state.provider_continuation_recovery_count = recovery_count + 1
                state.mark_transition(
                    retry_reason,
                    attempt=state.provider_continuation_recovery_count,
                )
                stream_text.reset_for_retry()
                stream_text.saw_final_answer_phase = False
                yield PostStreamRecoveryResult(
                    "retry", tool_batch_count, degraded_reason
                )
                return

        await run_stop_failure_hook(
            normalized_finish_reason,
            error_details=_PROVIDER_CONTINUATION_ERROR,
            last_assistant_message=stream_text.pending_recovery_text(scrub_text),
            hook_manager=context_builder.hook_manager,
        )
        yield AgentEvent.error(
            message=_PROVIDER_CONTINUATION_ERROR,
            recoverable=True,
            error_type="provider_continuation",
        )
        _set_terminal_reason(state, "provider_continuation", status="failed")
        yield usage_terminal_projection(
            turn_usage,
            status="failed",
            reason="provider_continuation",
        )
        yield PostStreamRecoveryResult("terminate", tool_batch_count, degraded_reason)
        return

    # Pi treats every tool call in an output-truncated assistant message as
    # potentially incomplete, even if a provider emitted a syntactically final
    # tool block before the terminal frame. Never commit or project such calls.
    # Claude Code's bounded continuation path then asks the provider to resume,
    # allowing it to re-issue any intended call with complete arguments.
    if is_max_output_finish_reason(finish_reason):
        recovery = await recover_max_output(
            state=state,
            stream_text=stream_text,
            tool_tracker=tool_tracker,
            context_builder=context_builder,
            budget_runtime=budget_runtime,
            provider_items=stream_state.response_items,
            turn_usage=turn_usage,
            finish_reason=finish_reason,
            scrub_text=scrub_text,
            run_stop_failure_hook=partial(
                run_stop_failure_hook,
                hook_manager=context_builder.hook_manager,
            ),
            provider_raw_done=stream_state.raw_done,
        )
        for event in recovery.events:
            yield event
        yield PostStreamRecoveryResult(
            recovery.action, tool_batch_count, degraded_reason
        )
        return

    if stream_state.saw_partial_tool_call and (
        not pending_tool_calls or not stream_state.final_tool_batch_received
    ):
        tool_tracker.cancel_remaining()
        yield AgentEvent.error(
            message="The provider stream ended before the tool arguments were complete.",
            recoverable=True,
            error_type="incomplete_tool_stream",
        )
        _set_terminal_reason(state, "incomplete_tool_stream", status="failed")
        yield PostStreamRecoveryResult("terminate", tool_batch_count, degraded_reason)
        return

    if not pending_tool_calls:
        imitated_tool = textual_tool_call_imitation(
            stream_text.accepted_answer_text(scrub_text),
            exposed_tool_names=set(exposed_tool_names or ()),
        )
        if imitated_tool:
            failed_message = stream_text.complete_active_agent_message(
                stream_text.active_agent_message_text,
                source="provider_protocol_error",
                status="failed",
                finish_reason="invalid_model_action",
            )
            if failed_message is not None:
                yield failed_message
            stream_text.clear_pending()
            state.mark_transition(
                "invalid_model_action",
                protocol_reason="textual_tool_call_imitation",
                tool_name=imitated_tool,
            )
            yield AgentEvent.error(
                message=(
                    f"模型把工具 {imitated_tool} 写成了普通 XML 文本，而不是结构化工具调用；"
                    "该工具没有执行，本轮结果未完成。请重试或切换支持原生工具调用的模型。"
                ),
                recoverable=True,
                error_type="invalid_model_action",
                error_code="textual_tool_call_imitation",
            )
            _set_terminal_reason(state, "invalid_model_action", status="failed")
            yield usage_terminal_projection(
                turn_usage,
                status="failed",
                reason="invalid_model_action",
            )
            yield PostStreamRecoveryResult(
                "terminate", tool_batch_count, degraded_reason
            )
            return

    yield PostStreamRecoveryResult("proceed", tool_batch_count, degraded_reason)
