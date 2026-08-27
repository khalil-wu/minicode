"""Typed internal terminal intent projected only by the outer agent loop."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from backend.agent.message import AgentEvent
from backend.agent.turn_kernel import _set_terminal_reason
from backend.llm.base import UsageInfo


def _snapshot_usage(usage: UsageInfo) -> UsageInfo:
    return UsageInfo(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_creation_input_tokens=usage.cache_creation_input_tokens,
        cache_read_input_tokens=usage.cache_read_input_tokens,
        cache_deleted_input_tokens=usage.cache_deleted_input_tokens,
        reasoning_output_tokens=usage.reasoning_output_tokens,
        input_includes_cache_read=usage.input_includes_cache_read,
        input_includes_cache_write=usage.input_includes_cache_write,
        ordinary_input_tokens=usage.ordinary_input_tokens,
        prompt_cache_total_tokens=usage.prompt_cache_total_tokens,
        cost_usd=usage.cost_usd,
    )


@dataclass(frozen=True, slots=True)
class TurnTerminalProjection:
    """Terminal evidence awaiting checkpoint finalization and durable commit."""

    usage: UsageInfo
    status: str
    reason: str = ""
    provider_raw: dict[str, Any] | None = None

    @classmethod
    def from_usage(
        cls,
        usage: UsageInfo,
        *,
        status: str,
        reason: str = "",
        provider_raw: dict[str, Any] | None = None,
    ) -> "TurnTerminalProjection":
        return cls(
            usage=_snapshot_usage(usage),
            status=str(status or "completed"),
            reason=str(reason or ""),
            provider_raw=(dict(provider_raw) if provider_raw else None),
        )

    def to_event(
        self,
        *,
        status: str,
        reason: str,
        checkpoint: dict[str, Any] | None = None,
    ) -> AgentEvent:
        usage = self.usage
        event = AgentEvent.done(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_creation_input_tokens=usage.cache_creation_input_tokens,
            cache_read_input_tokens=usage.cache_read_input_tokens,
            cache_deleted_input_tokens=usage.cache_deleted_input_tokens,
            reasoning_output_tokens=usage.reasoning_output_tokens,
            cost_usd=usage.cost_usd,
            input_includes_cache_read=usage.input_includes_cache_read,
            input_includes_cache_write=usage.input_includes_cache_write,
            ordinary_input_tokens=usage.normalized_ordinary_input_tokens,
            prompt_cache_total_tokens=usage.normalized_prompt_cache_total_tokens,
            provider_raw=self.provider_raw,
            status=status,
            reason=reason,
        )
        if checkpoint is not None:
            event.data["checkpoint"] = dict(checkpoint)
        return event


def terminal_status_and_reason(
    *,
    state: Any,
    terminal_projection: TurnTerminalProjection | None,
) -> tuple[str, str]:
    """Resolve the one public terminal status/reason pair for a turn."""
    status = str(
        state.terminal_status
        or (
            terminal_projection.status
            if terminal_projection is not None
            else "completed"
        )
    )
    reason = str(
        state.stopped_reason
        or (
            terminal_projection.reason
            if terminal_projection is not None
            else ""
        )
    )
    return status, "" if reason == "completed" else reason


def terminal_boundary_events(
    *,
    turn_kernel: Any,
    session_id: str,
    user_message: str,
    state: Any,
    context_builder: Any,
    usage: UsageInfo,
    terminal_projection: TurnTerminalProjection | None,
    status: str,
    reason: str,
) -> list[AgentEvent]:
    """Finalize checkpoint evidence and build the one loop terminal boundary."""
    checkpoint_status = turn_kernel.finalize_checkpoint(
        session_id=session_id,
        user_message=user_message,
        state=state,
        context_builder=context_builder,
        defer_completed_clear=True,
    )
    if checkpoint_status == "save_failed" and str(reason or "") != "checkpoint_save_failed":
        downgraded_reply = bool(str(getattr(state, "reply", "") or "").strip())
        status = "partial" if downgraded_reply else "failed"
        reason = "checkpoint_save_failed"
        _set_terminal_reason(state, reason, status=status)
    elif checkpoint_status in {"save_failed", "clear_failed"}:
        # A failed cleanup after a non-completed turn is durable evidence, not
        # terminal authority. The reason remains the provider/tool/budget fact.
        _set_terminal_reason(
            state,
            str(state.stopped_reason or "unknown"),
            status="failed",
        )
    checkpoint = turn_kernel.checkpoint_evidence()
    events: list[AgentEvent] = []
    events.append(
        AgentEvent(
            type="agent.terminal.intent",
            data={"status": status, "reason": reason, "checkpoint": checkpoint},
        )
    )
    projection = terminal_projection or TurnTerminalProjection.from_usage(
        usage,
        status=status,
        reason=reason,
    )
    events.append(
        projection.to_event(
            status=status,
            reason=reason,
            checkpoint=checkpoint,
        )
    )
    return events
