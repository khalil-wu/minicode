from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

from backend.agent.context import ContextBuilder
from backend.agent.context_ledger import ContextLedger, empty_context_ledger
from backend.agent.message import AgentEvent
from backend.agent.state import AgentState
from backend.config import TokenBudget
from backend.hooks.manager import HookEvent, get_hook_manager

logger = logging.getLogger(__name__)

POST_COMPACTION_HARD_LIMIT = 0.98
AUTO_COMPACT_DEFAULT_OUTPUT_RESERVE = 8_000
AUTO_COMPACT_MIN_OUTPUT_RESERVE = 4_000
AUTO_COMPACT_MAX_OUTPUT_RESERVE = 32_000


def _absolute_output_reserve(budget: TokenBudget) -> int:
    total = max(1, int(getattr(budget, "total", 0) or 0))
    configured = int(getattr(budget, "response_reserve", 0) or 0)
    reserve = configured if configured > 0 else AUTO_COMPACT_DEFAULT_OUTPUT_RESERVE
    # Unit tests and tiny synthetic budgets should still exercise the normal
    # percentage path; the absolute reserve is a guard for real context windows.
    total_cap = max(1, int(total * 0.2))
    return min(AUTO_COMPACT_MAX_OUTPUT_RESERVE, total_cap, reserve)


def _is_inside_output_reserve(ctx: ContextBuilder, budget: TokenBudget) -> bool:
    used = int(getattr(ctx, "token_usage", 0) or 0)
    reserve = _absolute_output_reserve(budget)
    return used >= max(0, int(getattr(budget, "total", 0) or 0) - reserve)


def _hook_manager_has_hooks(hook_mgr: Any, event: HookEvent) -> bool:
    has_hooks = getattr(hook_mgr, "has_hooks", None)
    if not callable(has_hooks):
        return False
    try:
        return bool(has_hooks(event))
    except Exception as exc:
        logger.debug("hook has_hooks(%s) failed: %s", event, exc)
        return False


async def manage_context_budget(
    ctx: ContextBuilder,
    state: AgentState,
    budget: TokenBudget,
    tool_schemas: list[dict[str, Any]],
) -> AsyncIterator[AgentEvent]:
    """Pre-call context pipeline: enforce tool-result budget, check budget, compact if needed.

    The tool-result budget runs *before* the compaction threshold check so that
    a sudden spike in tool output is shed immediately rather than triggering a
    full LLM-based compaction that may be too slow or too lossy for the situation.
    """
    # Tier 0: enforce global tool-result token budget (truncates oldest first).
    # This was previously dead code — apply_tool_result_budget was defined on
    # ContextBuilder but never called.  Wiring it here ensures that tool result
    # bloat is handled before it pushes the overall context into compaction.
    apply_budget = getattr(ctx, "apply_tool_result_budget", None)
    if callable(apply_budget):
        try:
            truncated = apply_budget()
            if truncated > 0:
                logger.info(
                    "[ToolResultBudget] Truncated %d tool results before compaction check",
                    truncated,
                )
        except Exception as exc:
            logger.debug("apply_tool_result_budget failed: %s", exc)

    # Tier 0.5: cheap local drain (snip old turns, collapse old tool previews)
    # before deciding whether an expensive full/LLM compact is required.
    # Order mirrors Claude Code query.ts: tool-budget -> snip -> collapse -> autocompact.
    usage_pct = getattr(ctx, "token_usage", 0) / max(budget.total, 1)
    apply_ladder = getattr(ctx, "apply_cheap_context_ladder", None)
    if callable(apply_ladder):
        try:
            ladder_stats = apply_ladder(usage_ratio=usage_pct) or {}
            snip_stats = ladder_stats.get("snip") if isinstance(ladder_stats, dict) else None
            collapse_stats = (
                ladder_stats.get("collapse") if isinstance(ladder_stats, dict) else None
            )
            snipped = int((snip_stats or {}).get("removed", 0) or 0)
            collapsed = int((collapse_stats or {}).get("collapsed", 0) or 0)
            saved = int(
                (ladder_stats.get("saved_tokens", 0) if isinstance(ladder_stats, dict) else 0)
                or 0
            )
            if snipped or collapsed:
                logger.info(
                    "[CheapContextLadder] snipped=%d collapsed=%d saved~%d tokens",
                    snipped,
                    collapsed,
                    saved,
                )
            # Recompute usage after local drain so full compact only runs if still needed.
            usage_pct = getattr(ctx, "token_usage", 0) / max(budget.total, 1)
        except Exception as exc:
            logger.debug("apply_cheap_context_ladder failed: %s", exc)

    try:
        should_compact = ctx.needs_compaction(state, tool_schemas=tool_schemas)
    except TypeError:
        should_compact = ctx.needs_compaction()

    reserve_triggered = _is_inside_output_reserve(ctx, budget)

    if usage_pct > 0.75 and not should_compact and not reserve_triggered:
        yield AgentEvent.budget_warning(
            bucket="total", percent=round(usage_pct, 3),
            will_compact=usage_pct > 0.85 or reserve_triggered,
        )

    if usage_pct >= 0.95 and hasattr(ctx, "full_compact"):
        async for event in _run_emergency_compaction(ctx, state, budget):
            yield event
        return

    if should_compact or reserve_triggered:
        async for event in _run_normal_compaction(ctx, state, budget):
            yield event


async def _run_emergency_compaction(
    ctx: ContextBuilder,
    state: AgentState,
    budget: TokenBudget,
) -> AsyncIterator[AgentEvent]:
    before_ledger = _context_ledger(ctx)
    hook_mgr = get_hook_manager()
    if hook_mgr and _hook_manager_has_hooks(hook_mgr, HookEvent.PRE_COMPACT):
        await hook_mgr.run_pre_compact()
    try:
        summary = await ctx.full_compact(restore_state=state)
    except TypeError:
        summary = await ctx.full_compact()
    logger.info("Emergency compaction: %s", summary[:120] if summary else "(empty)")
    yield _context_compacted_event(summary, before_ledger, _context_ledger(ctx))
    await _run_post_compact_hook()
    usage_pct = getattr(ctx, "token_usage", 0) / max(budget.total, 1)
    if usage_pct >= POST_COMPACTION_HARD_LIMIT:
        yield AgentEvent.error(
            message="紧急压缩后上下文仍然接近满载。请使用 /clear 或 /compact。",
            recoverable=True,
            error_type="budget",
        )
        state.stopped_reason = "budget_exceeded"


async def _run_normal_compaction(
    ctx: ContextBuilder,
    state: AgentState,
    budget: TokenBudget,
) -> AsyncIterator[AgentEvent]:
    state.consecutive_compaction_failures = max(
        state.consecutive_compaction_failures,
        max(0, int(getattr(ctx, "consecutive_compaction_failures", 0) or 0)),
    )
    before_ledger = _context_ledger(ctx)
    hook_mgr = get_hook_manager()
    if hook_mgr and _hook_manager_has_hooks(hook_mgr, HookEvent.PRE_COMPACT):
        await hook_mgr.run_pre_compact()
    try:
        summary = await ctx.compact(focus=state.user_message, restore_state=state)
    except TypeError:
        summary = await ctx.compact()
    logger.info("Compaction done: %s", summary[:80] if summary else "(empty)")
    yield _context_compacted_event(summary, before_ledger, _context_ledger(ctx))
    await _run_post_compact_hook()
    usage_pct = getattr(ctx, "token_usage", 0) / max(budget.total, 1)
    if usage_pct >= POST_COMPACTION_HARD_LIMIT:
        yield AgentEvent.error(
            message="压缩后上下文仍超过安全限制。请使用 /clear 或 /compact。",
            recoverable=True,
            error_type="budget",
        )
        state.stopped_reason = "budget_exceeded"
        return

    if usage_pct > 0.80:
        state.consecutive_compaction_failures += 1
        setattr(ctx, "consecutive_compaction_failures", state.consecutive_compaction_failures)
        if state.consecutive_compaction_failures >= 3:
            yield AgentEvent.error(
                message="连续 3 次压缩未能释放足够上下文。请使用 /clear 或 /compact。",
                recoverable=True,
                error_type="budget",
            )
            state.stopped_reason = "budget_exceeded"
            return
    else:
        state.consecutive_compaction_failures = 0
        setattr(ctx, "consecutive_compaction_failures", 0)


async def _run_post_compact_hook() -> None:
    hook_mgr = get_hook_manager()
    if not hook_mgr or not _hook_manager_has_hooks(hook_mgr, HookEvent.POST_COMPACT):
        return
    try:
        await hook_mgr.run_post_compact()
    except Exception as exc:
        logger.warning("post_compact hook failed: %s", exc)


def _context_ledger(ctx: ContextBuilder) -> ContextLedger:
    builder = getattr(ctx, "context_ledger", None)
    if callable(builder):
        try:
            ledger = builder()
            if isinstance(ledger, dict):
                return ledger  # type: ignore[return-value]
        except Exception as exc:
            logger.debug("context_ledger failed around compaction: %s", exc)
    usage = max(0, int(getattr(ctx, "token_usage", 0) or 0))
    return empty_context_ledger(estimated_tokens=usage, actual_tokens=usage)


def _context_compacted_event(
    summary: str,
    before_ledger: ContextLedger,
    after_ledger: ContextLedger,
) -> AgentEvent:
    def boundary_tokens(ledger: ContextLedger) -> int:
        """Use provider usage when known, otherwise the post-change estimate.

        Compaction invalidates the previous provider-observed prompt size, so a
        freshly compacted ledger intentionally reports ``actual_tokens == 0``
        until the next model request. The boundary still needs a useful
        before/after value rather than displaying zero for every compaction.
        """

        actual = int(ledger.get("actual_tokens", 0) or 0)
        if actual > 0:
            return actual
        return max(0, int(ledger.get("estimated_tokens", 0) or 0))

    retained_categories = [
        str(entry.get("category"))
        for entry in after_ledger.get("entries", [])
        if isinstance(entry, dict)
        and entry.get("category")
        and any(
            int(entry.get(key, 0) or 0) > 0
            for key in ("estimated_tokens", "item_count", "source_count")
        )
    ]
    return AgentEvent.context_compacted(
        summary=summary,
        before_tokens=boundary_tokens(before_ledger),
        after_tokens=boundary_tokens(after_ledger),
        retained_categories=retained_categories,
        ledger=after_ledger,
    )
