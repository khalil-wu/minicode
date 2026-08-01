"""Commit an accepted provider answer and its terminal state."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from backend.agent.answer_commit_projection import build_answer_commit_projection
from backend.agent.message import AgentEvent


async def commit_accepted_final_answer(
    *,
    candidate_text: str,
    state: Any,
    stream_text: Any,
    runtime: Any,
    run_record: Any,
    answer_committer: Any,
    degraded_reason: str,
    finish_reason: str,
    provider_raw_final_text: dict[str, Any],
    provider_raw_done: dict[str, Any],
    provider_phase: str,
    provider_items: list[dict[str, Any]],
    usage: Any,
) -> AsyncIterator[AgentEvent]:
    """Commit the model-authored answer without a hidden review call."""

    runtime.update_phase(run_record.run_id, "final", summary="Preparing final answer")
    projection = build_answer_commit_projection(
        stream_text=stream_text,
        final_text=candidate_text,
        finish_reason=finish_reason,
        provider_raw={**provider_raw_final_text, **provider_raw_done},
        degraded_reason=degraded_reason,
    )
    for event in projection.text_events:
        yield event
    for event in answer_committer.commit_history_and_terminal(
        projection=projection,
        final_text=candidate_text,
        provider_phase=provider_phase,
        provider_items=provider_items,
        usage=usage,
        provider_raw=provider_raw_done,
    ):
        yield event
