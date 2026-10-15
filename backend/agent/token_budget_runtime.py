"""The no-summary compaction lifecycle used by Codex TokenBudget tools."""

from __future__ import annotations

from collections.abc import AsyncIterator
import time
from uuid import uuid4

from backend.agent.context import ContextBuilder
from backend.agent.message import AgentEvent
from backend.agent.state import AgentState
from backend.hooks.manager import HookEvent
from backend.agent.context_compaction_hooks import (
    context_compaction_hook_events, run_context_compaction_hook, stop_context_compaction,
)
from backend.services.context_budget import build_context_compacted_event, context_ledger_snapshot


async def run_context_window_compaction(
    context: ContextBuilder, state: AgentState, *, trigger: str = "auto",
) -> AsyncIterator[AgentEvent]:
    outcome = await run_context_compaction_hook(context, HookEvent.PRE_COMPACT, trigger=trigger)
    for event in context_compaction_hook_events(outcome):
        yield event
    if outcome is not None and outcome.prevent_continuation:
        stop_context_compaction(state)
        return
    before = context_ledger_snapshot(context)
    before_tokens = context.context_window_status()["active_context_tokens"]
    item = {"id": "context-compaction-" + uuid4().hex, "type": "context_compaction",
            "source": trigger, "started_at": int(time.time() * 1_000)}
    yield AgentEvent("item.started", {"item": {**item, "status": "in_progress"}})
    context.start_new_context_window(state)
    event = build_context_compacted_event("", before, context_ledger_snapshot(context))
    event.data.update(
        before_tokens=before_tokens,
        after_tokens=context.context_window_status()["active_context_tokens"],
        context_window_state=context.export_snapshot()["context_window_state"],
        compaction_item_id=item["id"],
    )
    yield event
    # The consumer persists context_compacted before accepting completion.
    yield AgentEvent("item.completed", {"item": {**item, "status": "completed", "finished_at": int(time.time() * 1_000)}})
    outcome = await run_context_compaction_hook(context, HookEvent.POST_COMPACT, trigger=trigger)
    for event in context_compaction_hook_events(outcome):
        yield event
    if outcome is not None and outcome.prevent_continuation:
        stop_context_compaction(state)


async def settle_context_window(
    context: ContextBuilder, state: AgentState, *, needs_follow_up: bool,
) -> AsyncIterator[AgentEvent]:
    if not context.token_budget_enabled:
        return
    status = context.context_window_status()
    should_roll_over = needs_follow_up and (
        context.new_context_window_requested or status["token_limit_reached"]
    )
    context.record_context_window_reminders(
        allow_fallback=not should_roll_over and not status["token_limit_reached"],
    )
    if should_roll_over:
        async for event in run_context_window_compaction(context, state):
            yield event

