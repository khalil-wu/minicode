"""Post-stream terminal handling before final answer or tool execution."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal

from backend.agent.loop_preflight import run_stop_failure_hook
from backend.agent.loop_runtime_helpers import is_max_output_finish_reason
from backend.agent.max_output_recovery import recover_max_output
from backend.agent.message import AgentEvent
from backend.agent.turn_kernel import _set_terminal_reason


PostStreamAction = Literal["proceed", "retry", "terminate"]


@dataclass(frozen=True, slots=True)
class PostStreamRecoveryResult:
    action: PostStreamAction
    tool_batch_count: int
    degraded_reason: str


async def recover_provider_response(
    *,
    state: Any,
    stream_state: Any,
    stream_text: Any,
    tool_executor: Any,
    context_builder: Any,
    budget_runtime: Any,
    turn_usage: Any,
    finish_reason: str,
    scrub_text: Any,
    tool_batch_count: int,
    degraded_reason: str,
) -> AsyncIterator[AgentEvent | PostStreamRecoveryResult]:
    pending_tool_calls = stream_state.tool_calls

    # Pi treats every tool call in an output-truncated assistant message as
    # potentially incomplete, even if a provider emitted a syntactically final
    # tool block before the terminal frame. Never commit or project such calls.
    # Claude Code's bounded continuation path then asks the provider to resume,
    # allowing it to re-issue any intended call with complete arguments.
    if is_max_output_finish_reason(finish_reason):
        recovery = await recover_max_output(
            state=state,
            stream_text=stream_text,
            tool_executor=tool_executor,
            context_builder=context_builder,
            budget_runtime=budget_runtime,
            provider_items=stream_state.response_items,
            turn_usage=turn_usage,
            finish_reason=finish_reason,
            scrub_text=scrub_text,
            run_stop_failure_hook=run_stop_failure_hook,
        )
        for event in recovery.events:
            yield event
        yield PostStreamRecoveryResult(recovery.action, tool_batch_count, degraded_reason)
        return

    if stream_state.saw_partial_tool_call and (
        not pending_tool_calls or not stream_state.final_tool_batch_received
    ):
        tool_executor.cancel_remaining()
        yield AgentEvent.error(
            message="The provider stream ended before the tool arguments were complete.",
            recoverable=True,
            error_type="incomplete_tool_stream",
        )
        _set_terminal_reason(state, "incomplete_tool_stream", status="failed")
        yield PostStreamRecoveryResult("terminate", tool_batch_count, degraded_reason)
        return

    yield PostStreamRecoveryResult("proceed", tool_batch_count, degraded_reason)
