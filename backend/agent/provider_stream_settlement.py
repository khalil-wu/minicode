"""Settle provider stream state after event consumption has stopped."""

from __future__ import annotations

from collections.abc import AsyncIterator
import asyncio
from dataclasses import dataclass
from typing import Any, Literal

from backend.agent.message import AgentEvent
from backend.agent.provider_protocol import (
    add_usage,
)
from backend.agent.stream_sanitizer import scrub_thinking_tags
from backend.llm.base import UsageInfo


ProviderStreamAction = Literal["proceed", "retry", "terminate"]


@dataclass(frozen=True, slots=True)
class ProviderStreamSettlement:
    action: ProviderStreamAction
    turn_usage: UsageInfo
    finish_reason: str


async def settle_provider_stream(
    *,
    retry_budget_boundary: Any,
    budget_runtime: Any,
    turn_kernel: Any,
    provider_attempt: Any,
    finish_reason: str,
    provider_stream_steered: bool,
    rebuild_context_and_retry: bool,
    state: Any,
    pending_tool_calls: list[Any],
    provider_raw_done: dict[str, Any],
    provider_done: bool,
    visible_text_sanitizer: Any = None,
    stream_state: Any,
    stream_text: Any,
    context_builder: Any,
    usage: UsageInfo,
    turn_usage: UsageInfo,
    chain: Any,
) -> AsyncIterator[AgentEvent | ProviderStreamSettlement]:
    """Close provider lifecycle state and account usage."""

    if retry_budget_boundary is not None:
        _, events = await budget_runtime.apply_boundary(retry_budget_boundary)
        for event in events:
            yield event
        action: ProviderStreamAction = "terminate"
    else:
        await turn_kernel.close_provider_attempt(
            provider_attempt,
            status="completed" if provider_done else "failed",
            summary=(
                "Provider stream completed"
                if provider_done
                else "Provider stream ended without a terminal event"
            ),
            data={
                "finish_reason": finish_reason or "stream_exhausted",
                **({} if provider_done else {"error_type": "provider_terminal_missing"}),
            },
        )
        action = "proceed"

    if provider_stream_steered:
        action = "retry"
    elif rebuild_context_and_retry:
        state.iterations = max(0, state.iterations - 1)
        action = "retry"
    elif state.stopped_reason:
        action = "terminate"

    # Provider usage is spent even when steering, recovery, or a budget
    # boundary rejects the attempted response. Codex records every completed
    # sampling call into the shared rollout budget, not only accepted output.
    turn_usage = add_usage(turn_usage, usage)
    record_provider_usage_total = getattr(
        budget_runtime,
        "record_provider_usage_total",
        None,
    )
    if callable(record_provider_usage_total):
        record_provider_usage_total(turn_usage)
    chain.record_usage(
        input_tokens=usage.input_tokens or 0,
        output_tokens=usage.output_tokens or 0,
    )

    if action == "proceed":
        if pending_tool_calls and not provider_raw_done:
            finish_reason = finish_reason or "tool_calls_no_done"
            stream_state.finish_reason = finish_reason
        from backend.memory.citations import parse_memory_citation

        memory_citation = parse_memory_citation(
            list(getattr(visible_text_sanitizer, "citations", []) or [])
        )
        if memory_citation is not None:
            provider_raw_done["memory_citation"] = memory_citation
            recorder = getattr(context_builder, "record_memory_citation_usage", None)
            if callable(recorder):
                await asyncio.to_thread(
                    recorder,
                    list(memory_citation.get("rollout_ids") or []),
                )
        record_actual_usage = getattr(
            context_builder,
            "record_actual_usage",
            None,
        )
        if callable(record_actual_usage):
            record_actual_usage(usage, provider_raw=provider_raw_done)
        stream_text.sanitize(scrub_thinking_tags)
    yield ProviderStreamSettlement(
        action=action,
        turn_usage=turn_usage,
        finish_reason=finish_reason,
    )
