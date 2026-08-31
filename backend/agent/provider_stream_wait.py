"""Bounded acquisition of the next provider stream event."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal

from backend.agent.first_byte_waiter import wait_for_provider_event
from backend.agent.loop_preflight import PhaseDeadlineExceeded


@dataclass(frozen=True, slots=True)
class ProviderWaitResult:
    action: Literal["event", "finish"]
    event: Any | None = None
    finish_reason: str = ""
    response_phase: str = ""


async def wait_for_next_provider_event(
    *,
    stream_iter: Any,
    first_event: bool,
    settings: Any,
    budget_runtime: Any,
    tool_context: Any,
    stream_state: Any,
    pending_tool_calls: list[Any],
    awaiting_trailing_tool_done: bool,
) -> AsyncIterator[ProviderWaitResult]:
    """Finish with one bounded provider-stream acquisition result."""

    provider_wait_capped_by_phase_deadline = False
    del pending_tool_calls, stream_state, awaiting_trailing_tool_done

    configured_stream_timeout = getattr(settings, "stream_timeout_seconds", 0.0)
    timeout = (
        None
        if configured_stream_timeout in (None, 0, 0.0)
        else max(0.0, float(configured_stream_timeout))
    )
    try:
        if first_event:
            configured_first_byte_timeout = getattr(settings, "first_byte_timeout_seconds", 0.0)
            first_byte_timeout = (
                None
                if configured_first_byte_timeout in (None, 0, 0.0)
                else max(0.0, float(configured_first_byte_timeout))
            )
            if first_byte_timeout is not None:
                timeout = (
                    first_byte_timeout
                    if timeout is None
                    else min(timeout, first_byte_timeout)
                )
        timeout, provider_wait_capped_by_phase_deadline = (
            budget_runtime.bounded_provider_timeout(timeout)
        )
        event = await wait_for_provider_event(
            stream_iter,
            timeout_seconds=timeout,
            cancel_event=tool_context.cancel_event,
            owner=tool_context.pending_provider_tasks,
        )
    except PhaseDeadlineExceeded:
        raise
    except StopAsyncIteration:
        yield ProviderWaitResult(action="finish")
        return
    except asyncio.TimeoutError:
        if provider_wait_capped_by_phase_deadline:
            raise PhaseDeadlineExceeded
        raise

    yield ProviderWaitResult(action="event", event=event)
