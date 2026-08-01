"""Build one provider request context and its cache-stability metadata."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from backend.agent.loop_runtime_helpers import epoch_ms
from backend.agent.prompt_cache import (
    build_prompt_cache_safe_params,
    prompt_cache_fork_diagnostic,
)
from backend.agent.provider_protocol import (
    annotate_request_metadata_with_prompt_cache_fork,
)
from backend.llm.base import LLMAdapter, LLMMessage


@dataclass(frozen=True, slots=True)
class PreparedTurnContext:
    messages: list[LLMMessage]
    prompt_cache_safe_params: dict[str, Any]
    prompt_cache_fork: dict[str, Any] | None


async def prepare_turn_context(
    *,
    context: Any,
    state: Any,
    llm: LLMAdapter,
    tool_schemas: list[dict[str, Any]],
    request_metadata: dict[str, Any],
    metadata: dict[str, Any],
    external_metadata: dict[str, Any] | None,
    tool_context: Any,
    turn_kernel: Any,
    run_id: str,
) -> PreparedTurnContext:
    """Reconcile history, render messages, and project cache-fork metadata."""
    reconcile = getattr(context, "reconcile_dangling_tool_calls", None)
    if callable(reconcile):
        reconcile()

    span_id = f"context:{run_id}:{state.iterations + 1}"
    started_at = epoch_ms()
    await turn_kernel.emit_runtime_span(
        "context.build.started",
        span_id=span_id,
        phase="context",
        status="running",
        label="context",
        summary="Building model context",
        started_at=started_at,
        ui_visible=False,
    )
    messages = await context.build(state)

    completed_at = epoch_ms()
    await turn_kernel.emit_runtime_span(
        "context.build.completed",
        span_id=span_id,
        phase="context",
        status="completed",
        label="context",
        summary="Model context ready",
        started_at=started_at,
        ended_at=completed_at,
        duration_ms=completed_at - started_at,
        ui_visible=False,
        data={"message_count": len(messages)},
    )

    section_summary = state.prompt_context.get("prompt_section_summary")
    safe_params = build_prompt_cache_safe_params(
        messages=messages,
        tool_schemas=tool_schemas,
        request_metadata=request_metadata,
        prompt_section_summary=(
            dict(section_summary) if isinstance(section_summary, dict) else {}
        ),
    )
    fork = prompt_cache_fork_diagnostic(
        metadata.get("parent_prompt_cache_safe_params"),
        safe_params,
    )
    if fork:
        annotate_request_metadata_with_prompt_cache_fork(request_metadata, fork)
        safe_params = {**safe_params, "fork_context": fork}
        metadata["prompt_cache_fork"] = fork

    metadata["prompt_cache_safe_params"] = safe_params
    tool_context.metadata["prompt_cache_safe_params"] = safe_params
    if fork:
        tool_context.metadata["prompt_cache_fork"] = fork
    if external_metadata is not None:
        external_metadata["prompt_cache_safe_params"] = dict(safe_params)
        if fork:
            external_metadata["prompt_cache_fork"] = dict(fork)

    return PreparedTurnContext(
        messages=messages,
        prompt_cache_safe_params=safe_params,
        prompt_cache_fork=fork,
    )
