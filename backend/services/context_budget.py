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
    """Run Pi-style automatic compaction at the configured reserve boundary."""
    del budget
    if ctx.needs_compaction(state, tool_schemas=tool_schemas):
        async for event in _run_normal_compaction(ctx, state, tool_schemas):
            yield event


async def _run_normal_compaction(
    ctx: ContextBuilder,
    state: AgentState,
    tool_schemas: list[dict[str, Any]],
) -> AsyncIterator[AgentEvent]:
    before_ledger = _context_ledger(ctx)
    hook_mgr = get_hook_manager()
    if hook_mgr and _hook_manager_has_hooks(hook_mgr, HookEvent.PRE_COMPACT):
        await hook_mgr.run_pre_compact(trigger="auto")
    try:
        summary = await ctx.compact(focus=state.user_message, restore_state=state)
    except Exception as exc:
        logger.warning("Compaction failed: %s", exc)
        state.stopped_reason = "budget_exceeded"
        yield AgentEvent.error(
            message="上下文压缩失败。请重试或使用 /clear。",
            recoverable=True,
            error_type="budget",
        )
        return
    logger.info("Compaction done: %s", summary[:80] if summary else "(empty)")
    yield _context_compacted_event(summary, before_ledger, _context_ledger(ctx))
    await _run_post_compact_hook(summary)
    if ctx.needs_compaction(state, tool_schemas=tool_schemas):
        yield AgentEvent.error(
            message="压缩后上下文仍超过模型窗口。请使用 /clear。",
            recoverable=True,
            error_type="budget",
        )
        state.stopped_reason = "budget_exceeded"


async def _run_post_compact_hook(summary: str) -> None:
    hook_mgr = get_hook_manager()
    if not hook_mgr or not _hook_manager_has_hooks(hook_mgr, HookEvent.POST_COMPACT):
        return
    try:
        await hook_mgr.run_post_compact(summary=summary, trigger="auto")
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
