"""Atomic settlement of a provider DONE frame."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from backend.agent.cache_metrics import (
    args_signature,
    cache_metric_event,
    prompt_cache_saved_ms,
)
from backend.agent.message import AgentEvent
from backend.agent.prompt_cache import observe_prompt_cache_break
from backend.agent.provider_protocol import (
    loop_metrics_payload,
    merge_prompt_cache_safe_request_summary,
    provider_trace_payload,
)
from backend.agent.stream_attempt import StreamAttemptState
from backend.llm.base import StreamEvent, UsageInfo


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ProviderCompletionResult:
    usage: UsageInfo
    finish_reason: str
    response_phase: str
    events: tuple[AgentEvent, ...]


class ProviderCompletionCoordinator:
    """Commit usage, cache, trace, and attempt metadata as one DONE action."""

    def __init__(
        self,
        *,
        settings: Any,
        state: Any,
        turn_kernel: Any,
        prompt_cache_tracking_source: str,
        turn_started_at: float,
        turn_start_tool_call_count: int,
    ) -> None:
        self.settings = settings
        self.state = state
        self.turn_kernel = turn_kernel
        self.prompt_cache_tracking_source = prompt_cache_tracking_source
        self.turn_started_at = turn_started_at
        self.turn_start_tool_call_count = turn_start_tool_call_count

    async def settle(
        self,
        event: StreamEvent,
        *,
        stream_state: StreamAttemptState,
        provider_attempt: Any,
        prompt_cache_safe_params: dict[str, Any],
        iteration_id: str,
        iteration_limit: int,
        tool_batch_count: int,
    ) -> ProviderCompletionResult:
        stream_state.accept_provider_event(event)
        usage = stream_state.usage
        finish_reason = stream_state.finish_reason
        raw_done = stream_state.raw_done
        if prompt_cache_safe_params:
            raw_done["prompt_cache_safe_params"] = dict(prompt_cache_safe_params)
        raw_done["request_summary"] = merge_prompt_cache_safe_request_summary(
            raw_done.get("request_summary"),
            prompt_cache_safe_params,
        )
        raw_done["loop_metrics"] = loop_metrics_payload(
            turn_started_at=self.turn_started_at,
            state=self.state,
            provider_call_count=self.turn_kernel.next_provider_call_count,
            iteration_limit=iteration_limit,
            iteration_hard_limit=iteration_limit,
            tool_batch_count=tool_batch_count,
            turn_start_tool_call_count=self.turn_start_tool_call_count,
            pending_tool_call_count=(
                len(stream_state.tool_calls)
                if stream_state.tool_calls and stream_state.final_tool_batch_received
                else 0
            ),
        )

        events: list[AgentEvent] = []
        diagnostic = observe_prompt_cache_break(
            request_summary=raw_done.get("request_summary"),
            usage=usage,
            source=self.prompt_cache_tracking_source,
        )
        if diagnostic:
            raw_done["prompt_cache_diagnostic"] = diagnostic
            logger.warning("[PromptCacheBreak] %s", diagnostic.get("reason", "unknown"))

        cache_read_tokens = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        cache_creation_tokens = int(
            getattr(usage, "cache_creation_input_tokens", 0) or 0
        )
        if cache_read_tokens or cache_creation_tokens:
            events.append(
                cache_metric_event(
                    cache_layer="provider.prompt",
                    tool_name="provider",
                    run_id=iteration_id,
                    turn_id=iteration_id,
                    args_signature_value=args_signature(raw_done.get("request_summary") or {}),
                    hit=cache_read_tokens > 0,
                    estimated_saved_ms=prompt_cache_saved_ms(cache_read_tokens),
                    payload_size_bytes=(cache_read_tokens + cache_creation_tokens) * 4,
                )
            )

        call_index, trace_id = self.turn_kernel.commit_provider_call(iteration_id)
        raw_done.setdefault("trace_id", trace_id)
        events.append(
            AgentEvent.inspector_update(
                "provider",
                trace_id,
                provider_trace_payload(
                    provider_raw=raw_done,
                    usage=usage,
                    finish_reason=finish_reason,
                    iteration_id=iteration_id,
                    call_index=call_index,
                    loop_metrics=raw_done.get("loop_metrics"),
                ),
            )
        )
        await self.turn_kernel.close_provider_attempt(
            provider_attempt,
            status="completed",
            summary="Provider request completed",
            data={
                "finish_reason": finish_reason,
                "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
                "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
            },
        )
        return ProviderCompletionResult(
            usage=usage,
            finish_reason=finish_reason,
            response_phase=stream_state.response_phase,
            events=tuple(events),
        )
