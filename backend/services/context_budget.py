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


def build_context_budget_snapshot(session: Any, builder: ContextBuilder) -> dict[str, Any]:
    """Build the same authoritative budget projection for every compact path."""
    state = getattr(session, "_last_agent_state", None)
    if state is None:
        state = AgentState(user_message="")
    tool_schemas = None
    try:
        tool_schemas = session.tool_registry.get_schemas(
            permission_checker=session.permission_checker,
            permission_context=session.permission_context,
        )
    except Exception:
        tool_schemas = None
    return builder.get_budget_snapshot(state=state, tool_schemas=tool_schemas)

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
    """Run automatic compaction at the configured reserve boundary.

    A bounded circuit breaker stops repeated failed compactions after three
    consecutive attempts.
    After 3 consecutive compaction failures we stop auto-compacting and let
    the turn terminate with a budget_exceeded error so the user can /clear.
    Without this guard a context that cannot be reduced below the threshold
    will loop indefinitely, burning API calls.
    """
    if not ctx.needs_compaction(state, tool_schemas=tool_schemas):
        warning = _pre_compaction_warning(ctx, state, budget, tool_schemas)
        if warning is not None:
            yield warning
        return
    state.budget_warning_emitted = False

    # Consecutive-failure circuit breaker: three failed attempts open it.
    MAX_CONSECUTIVE_FAILURES = 3
    if state.consecutive_autocompact_failures >= MAX_CONSECUTIVE_FAILURES:
        logger.warning(
            "Auto-compact circuit breaker: %d consecutive failures; "
            "stopping auto-compaction. Use /clear to reset.",
            state.consecutive_autocompact_failures,
        )
        state.stopped_reason = "budget_exceeded"
        yield AgentEvent.error(
            message=(
                "上下文持续超出窗口，自动压缩已停止。请使用 /clear 清除对话后继续。"
            ),
            recoverable=True,
            error_type="budget",
            error_code="autocompact_circuit_open",
        )
        return

    async for event in _run_normal_compaction(ctx, state, tool_schemas):
        yield event


# Width of the advisory band below the compaction boundary, matching cc's
# WARNING_THRESHOLD_BUFFER_TOKENS. Inside it the turn still runs normally; the
# user is only told that the next few turns will trigger compaction.
_WARNING_BAND_TOKENS = 20_000


def _pre_compaction_warning(
    ctx: ContextBuilder,
    state: AgentState,
    budget: TokenBudget,
    tool_schemas: list[dict[str, Any]],
) -> AgentEvent | None:
    """Announce the approaching compaction boundary exactly once per band.

    ``budget.warning`` had a complete transport and renderer but no producer, so
    compaction always arrived unannounced. Emitting from here keeps the trigger
    arithmetic beside the only caller that decides to compact.
    """

    trigger = budget.total - budget.response_reserve
    band_floor = trigger - _WARNING_BAND_TOKENS
    if band_floor <= 0:
        # The reserve alone fills the window, so there is no span between
        # "roomy" and "compacting" to warn about.
        return None
    snapshot = ctx.get_budget_snapshot(state, tool_schemas=tool_schemas)
    total = int(snapshot.get("total", 0) or 0)
    if total <= 0:
        return None
    used = int(snapshot.get("used", 0) or 0)
    if used < band_floor:
        # Left the band (a compaction or /clear freed room); re-arm the notice.
        state.budget_warning_emitted = False
        return None
    if state.budget_warning_emitted:
        return None
    state.budget_warning_emitted = True
    return AgentEvent.budget_warning(
        "context",
        min(1.0, used / total),
        will_compact=True,
    )


async def _run_normal_compaction(
    ctx: ContextBuilder,
    state: AgentState,
    tool_schemas: list[dict[str, Any]],
) -> AsyncIterator[AgentEvent]:
    before_ledger = context_ledger_snapshot(ctx)
    hook_mgr = get_hook_manager()
    pre_compact_result = None
    if hook_mgr and _hook_manager_has_hooks(hook_mgr, HookEvent.PRE_COMPACT):
        try:
            pre_compact_result = await hook_mgr.run_pre_compact(trigger="auto")
        except Exception as exc:
            # A hook execution failure is observable, but it must not turn an
            # otherwise recoverable compaction into an unhandled task error.
            logger.warning("pre_compact hook failed: %s", exc)
            pre_compact_result = None

        # Hook feedback is advisory for compaction. A blocking hook result is
        # recorded below, but cannot veto the recovery operation or wedge every
        # future turn at the budget boundary.
        if pre_compact_result is not None and getattr(pre_compact_result, "blocked", False):
            message = str(
                getattr(pre_compact_result, "message", "")
                or getattr(pre_compact_result, "feedback", "")
                or ""
            ).strip()
            if message:
                logger.warning("PreCompact hook message (non-blocking): %s", message)

    # A PreCompact hook may supply a focused instruction for the summarizer.
    # ContextBuilder's mature API exposes one focus string, so preserve the
    # normal user focus and append the hook instruction at the boundary rather
    # than dropping it.
    focus = state.user_message
    custom_instructions = str(
        getattr(pre_compact_result, "custom_instructions", "") or ""
    ).strip()
    if custom_instructions:
        focus = f"{focus}\n\n{custom_instructions}" if focus else custom_instructions
    try:
        summary = await ctx.compact(focus=focus, restore_state=state)
    except Exception as exc:
        logger.warning("Compaction failed: %s", exc)
        # Feed the circuit breaker in manage_context_budget.
        state.consecutive_autocompact_failures += 1
        state.stopped_reason = "budget_exceeded"
        yield AgentEvent.error(
            message="上下文压缩失败。请重试或使用 /clear。",
            recoverable=True,
            error_type="budget",
        )
        return
    # Successful compaction resets the breaker.
    state.consecutive_autocompact_failures = 0
    logger.info("Compaction done: %s", summary[:80] if summary else "(empty)")
    yield build_context_compacted_event(summary, before_ledger, context_ledger_snapshot(ctx))
    post_compact_result = await _run_post_compact_hook(summary)
    if post_compact_result is not None:
        # PostCompact is non-blocking, but its context/system fields are part
        # of the next model request.  Inject them into the same ContextBuilder
        # used by the loop so they survive the compaction boundary.
        additional_context = str(
            getattr(post_compact_result, "additional_context", "") or ""
        ).strip()
        if additional_context:
            append_user_context = getattr(ctx, "append_user_context", None)
            if callable(append_user_context):
                append_user_context(additional_context)
        system_message = str(
            getattr(post_compact_result, "system_message", "") or ""
        ).strip()
        if system_message:
            append_system_note = getattr(ctx, "append_system_note", None)
            if callable(append_system_note):
                append_system_note(system_message)
    if ctx.needs_compaction(state, tool_schemas=tool_schemas):
        yield AgentEvent.error(
            message="压缩后上下文仍超过模型窗口。请使用 /clear。",
            recoverable=True,
            error_type="budget",
        )
        state.stopped_reason = "budget_exceeded"


async def _run_post_compact_hook(summary: str) -> Any | None:
    hook_mgr = get_hook_manager()
    if not hook_mgr or not _hook_manager_has_hooks(hook_mgr, HookEvent.POST_COMPACT):
        return None
    try:
        return await hook_mgr.run_post_compact(summary=summary, trigger="auto")
    except Exception as exc:
        logger.warning("post_compact hook failed: %s", exc)
        return None


def context_ledger_snapshot(ctx: ContextBuilder) -> ContextLedger:
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


def build_context_compacted_event(
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
