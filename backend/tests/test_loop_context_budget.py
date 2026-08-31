import asyncio

from backend.agent.state import AgentState
from backend.config import TokenBudget
from backend.services.context_budget import manage_context_budget
from backend.hooks.manager import HookEvent, HookResult


async def _collect(ctx, state, budget):
    return [
        event
        async for event in manage_context_budget(ctx, state, budget, tool_schemas=[])
    ]


class _BudgetCtx:
    def __init__(self, *, token_usage: int, after_compact_usage: int | None = None) -> None:
        self.token_usage = token_usage
        self.after_compact_usage = after_compact_usage
        self.compact_calls = 0
        self.consecutive_autocompact_failures = 0
        self.hook_manager = None

    def record_autocompact_failure(self):
        self.consecutive_autocompact_failures += 1
        return self.consecutive_autocompact_failures

    def reset_autocompact_failures(self):
        self.consecutive_autocompact_failures = 0

    def needs_compaction(self, state=None, *, tool_schemas=None):
        return self.token_usage > 850

    async def compact(self, *args, **kwargs):
        self.compact_calls += 1
        if self.after_compact_usage is not None:
            self.token_usage = self.after_compact_usage
        return "normal summary"

    def context_ledger(self):
        return {
            "estimated_tokens": self.token_usage,
            "actual_tokens": self.token_usage,
            "compaction_count": self.compact_calls,
            "entries": [{
                "category": "history",
                "label": "History",
                "estimated_tokens": self.token_usage,
                "item_count": 1,
                "source_count": 0,
                "sources": [],
            }],
        }


def test_manage_context_budget_does_nothing_below_pi_reserve_boundary() -> None:
    ctx = _BudgetCtx(token_usage=760)
    state = AgentState(user_message="continue")

    events = asyncio.run(_collect(ctx, state, TokenBudget(total=1000)))

    assert events == []
    assert ctx.compact_calls == 0


def test_manage_context_budget_stops_immediately_if_compaction_is_insufficient() -> None:
    ctx = _BudgetCtx(token_usage=900, after_compact_usage=870)
    state = AgentState(user_message="continue")

    events = asyncio.run(_collect(ctx, state, TokenBudget(total=1000)))

    assert [event.type for event in events] == ["context_compacted", "error"]
    assert events[0].data["before_tokens"] == 900
    assert events[0].data["after_tokens"] == 870
    assert events[0].data["retained_categories"] == ["history"]
    assert state.stopped_reason == "budget_exceeded"
    assert ctx.compact_calls == 1


def test_manage_context_budget_emits_one_compaction_when_reserve_is_restored() -> None:
    ctx = _BudgetCtx(token_usage=900, after_compact_usage=500)
    state = AgentState(user_message="continue")

    events = asyncio.run(_collect(ctx, state, TokenBudget(total=1000)))

    assert [event.type for event in events] == ["context_compacted"]
    assert ctx.compact_calls == 1


def test_autocompact_failure_counter_is_owned_by_context_across_turn_states() -> None:
    class _FailingCtx(_BudgetCtx):
        async def compact(self, *args, **kwargs):
            self.compact_calls += 1
            raise RuntimeError("cannot compact")

    ctx = _FailingCtx(token_usage=900)
    for _ in range(3):
        events = asyncio.run(
            _collect(ctx, AgentState(user_message="continue"), TokenBudget(total=1000))
        )
        assert events[-1].type == "error"

    assert ctx.consecutive_autocompact_failures == 3
    fourth = asyncio.run(
        _collect(ctx, AgentState(user_message="continue"), TokenBudget(total=1000))
    )
    assert fourth[0].data["error_code"] == "autocompact_circuit_open"
    assert ctx.compact_calls == 3


def test_compaction_boundary_uses_estimate_until_post_compaction_actual_is_known() -> None:
    class _EstimatedAfterCompactCtx(_BudgetCtx):
        def context_ledger(self):
            ledger = super().context_ledger()
            ledger["actual_tokens"] = self.token_usage if self.compact_calls == 0 else 0
            return ledger

    ctx = _EstimatedAfterCompactCtx(token_usage=900, after_compact_usage=500)
    state = AgentState(user_message="continue")

    events = asyncio.run(_collect(ctx, state, TokenBudget(total=1000)))

    assert events[0].type == "context_compacted"
    assert events[0].data["before_tokens"] == 900
    assert events[0].data["after_tokens"] == 500
    assert events[0].data["ledger"]["actual_tokens"] == 0


def test_pre_compact_blocked_is_reported_without_vetoing_compaction() -> None:
    """cc's PreCompact exit-code-2 never vetoes compaction (hooks.ts); the
    message is recorded and the context is compacted anyway."""

    class _HookManager:
        def has_hooks(self, event):
            return event == HookEvent.PRE_COMPACT

        async def run_pre_compact(self, **_kwargs):
            return HookResult(blocked=True, message="keep the original context")

    ctx = _BudgetCtx(token_usage=900, after_compact_usage=500)
    ctx.hook_manager = _HookManager()
    state = AgentState(user_message="continue")

    events = asyncio.run(_collect(ctx, state, TokenBudget(total=1000)))

    assert ctx.compact_calls == 1
    assert "pre_compact_blocked" not in [event.data.get("error_code") for event in events]
    assert state.stopped_reason != "budget_exceeded"


def test_pre_and_post_compact_hook_context_is_preserved() -> None:
    class _HookAwareCtx(_BudgetCtx):
        def __init__(self):
            super().__init__(token_usage=900, after_compact_usage=500)
            self.user_context: list[str] = []
            self.system_notes: list[str] = []

        def append_user_context(self, content: str) -> None:
            self.user_context.append(content)

        def append_system_note(self, content: str) -> None:
            self.system_notes.append(content)

    class _HookManager:
        def has_hooks(self, event):
            return event in {HookEvent.PRE_COMPACT, HookEvent.POST_COMPACT}

        async def run_pre_compact(self, **_kwargs):
            return HookResult(custom_instructions="retain the API contract")

        async def run_post_compact(self, **_kwargs):
            return HookResult(
                additional_context="post-compact context",
                system_message="post-compact system note",
            )

    ctx = _HookAwareCtx()
    ctx.hook_manager = _HookManager()
    state = AgentState(user_message="continue")
    compact_focus: list[str] = []

    async def compact(*args, **kwargs):
        del args
        compact_focus.append(str(kwargs.get("focus") or ""))
        ctx.compact_calls += 1
        ctx.token_usage = 500
        return "summary"

    ctx.compact = compact
    events = asyncio.run(_collect(ctx, state, TokenBudget(total=1000)))

    assert events and events[0].type == "context_compacted"
    assert compact_focus == ["continue\n\nretain the API contract"]
    assert ctx.user_context == ["post-compact context"]
    assert ctx.system_notes == ["post-compact system note"]


class _SnapshotCtx(_BudgetCtx):
    """A ctx that answers the budget snapshot the warning band needs."""

    def __init__(self, *, used: int, total: int) -> None:
        super().__init__(token_usage=used)
        self.used = used
        self.total = total

    def needs_compaction(self, state=None, *, tool_schemas=None):
        return self.used > self.total - 16_384

    def get_budget_snapshot(self, state=None, *, tool_schemas=None):
        return {"used": self.used, "total": self.total, "breakdown": {}}


def test_budget_warning_is_emitted_once_inside_the_pre_compaction_band() -> None:
    budget = TokenBudget(total=200_000)
    # 200000 - 16384 reserve - 20000 band = 163616 is the band floor.
    ctx = _SnapshotCtx(used=170_000, total=200_000)
    state = AgentState(user_message="continue")

    first = asyncio.run(_collect(ctx, state, budget))
    assert [event.type for event in first] == ["budget.warning"]
    assert first[0].data["will_compact"] is True
    assert first[0].data["bucket"] == "context"
    assert ctx.compact_calls == 0

    # Still in the band on the next turn: no repeated toast.
    assert asyncio.run(_collect(ctx, state, budget)) == []


def test_budget_warning_stays_silent_below_the_band_and_re_arms() -> None:
    budget = TokenBudget(total=200_000)
    ctx = _SnapshotCtx(used=100_000, total=200_000)
    state = AgentState(user_message="continue")

    assert asyncio.run(_collect(ctx, state, budget)) == []

    ctx.used = 170_000
    assert [e.type for e in asyncio.run(_collect(ctx, state, budget))] == ["budget.warning"]

    # Compaction freed room; leaving the band re-arms the notice.
    ctx.used = 100_000
    assert asyncio.run(_collect(ctx, state, budget)) == []
    assert state.budget_warning_emitted is False

    ctx.used = 170_000
    assert [e.type for e in asyncio.run(_collect(ctx, state, budget))] == ["budget.warning"]
