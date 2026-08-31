import math
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Any


# cc modelCost.ts pricing tiers ($/Mtok). Used ONLY when the provider
# payload carries no explicit cost, exactly like cc's getModelCosts fallback.
_COST_TIER_3_15 = {"input": 3.0, "output": 15.0, "cache_write": 3.75, "cache_read": 0.3}
_COST_TIER_15_75 = {"input": 15.0, "output": 75.0, "cache_write": 18.75, "cache_read": 1.5}
_COST_TIER_5_25 = {"input": 5.0, "output": 25.0, "cache_write": 6.25, "cache_read": 0.5}
_COST_HAIKU_35 = {"input": 0.8, "output": 4.0, "cache_write": 1.0, "cache_read": 0.08}
_COST_HAIKU_45 = {"input": 1.0, "output": 5.0, "cache_write": 1.25, "cache_read": 0.1}

# Substring matching mirrors cc's canonical-name table for relayed model ids.
_COST_MODEL_RULES: tuple[tuple[str, dict[str, float]], ...] = (
    ("haiku-4-5", _COST_HAIKU_45),
    ("haiku-3-5", _COST_HAIKU_35),
    ("opus-4-6", _COST_TIER_5_25),
    ("opus-4-5", _COST_TIER_5_25),
    ("opus-4-1", _COST_TIER_15_75),
    ("opus-4", _COST_TIER_15_75),
    ("sonnet-4", _COST_TIER_3_15),
    ("sonnet-3-7", _COST_TIER_3_15),
    ("sonnet-3-5", _COST_TIER_3_15),
)


def _estimate_cost_usd(
    model_id: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_creation_input_tokens: int,
    cache_read_input_tokens: int,
) -> float | None:
    """cc getModelCosts local fallback when the provider sends no cost.

    Returns ``None`` — not ``0.0`` — for a model this table cannot price.
    ``0.0`` is indistinguishable from "this request really was free", which
    made every OpenAI/DeepSeek/gateway turn look like it cost nothing.
    """
    name = str(model_id or "").lower()
    tier = next((rule for key, rule in _COST_MODEL_RULES if key in name), None)
    if tier is None:
        return None
    return (
        (input_tokens / 1_000_000) * tier["input"]
        + (output_tokens / 1_000_000) * tier["output"]
        + (cache_creation_input_tokens / 1_000_000) * tier["cache_write"]
        + (cache_read_input_tokens / 1_000_000) * tier["cache_read"]
    )


def estimate_usage_cost_usd(model_id: str, usage: Any) -> float | None:
    """Price one UsageInfo with the local table, or None if unpriceable.

    The turn-budget boundary needs the same table the session tracker uses, but
    it works from a UsageInfo rather than loose token counts. Returning None
    keeps "unpriced model" distinguishable from "this turn really was free".
    """
    return _estimate_cost_usd(
        model_id,
        input_tokens=_nonnegative_int(getattr(usage, "input_tokens", 0)),
        output_tokens=_nonnegative_int(getattr(usage, "output_tokens", 0)),
        cache_creation_input_tokens=_nonnegative_int(
            getattr(usage, "cache_creation_input_tokens", 0)
        ),
        cache_read_input_tokens=_nonnegative_int(
            getattr(usage, "cache_read_input_tokens", 0)
        ),
    )


def _nonnegative_int(value: Any) -> int:
    """Normalize provider counters without allowing malformed deltas to hurt totals."""
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _nonnegative_finite_float(value: Any) -> float:
    """Normalize an optional provider cost/duration to a safe finite value."""
    if isinstance(value, bool):
        return 0.0
    try:
        parsed = float(value or 0.0)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return parsed if math.isfinite(parsed) and parsed > 0 else 0.0


@dataclass
class CostTrackerState:
    input_tokens: int = 0
    ordinary_input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    prompt_cache_total_tokens: int = 0
    reasoning_output_tokens: int = 0
    total_cost_usd: float = 0.0
    total_duration_sec: float = 0.0
    priced_requests: int = 0
    # Requests whose cost is unknown: the provider reported none and the local
    # price table does not cover the model. ``total_cost_usd`` is a subtotal,
    # not a total, whenever this is non-zero.
    unpriced_requests: int = 0

class CostTracker:
    """
    Global Token & Cost Tracker (similar to claude-code cost-tracker.ts).
    Records accumulated API costs and duration across the entire runtime.
    """
    _instance: "CostTracker | None" = None

    def __init__(self):
        self.state = CostTrackerState()
        self._session_states: dict[str, CostTrackerState] = defaultdict(CostTrackerState)
        self._start_time = time.monotonic()

    @classmethod
    def get_instance(cls) -> "CostTracker":
        if cls._instance is None:
            cls._instance = CostTracker()
        return cls._instance

    def record_usage(
        self,
        input_tokens: int,
        output_tokens: int,
        cache_creation_input_tokens: int = 0,
        cache_read_input_tokens: int = 0,
        reasoning_output_tokens: int = 0,
        elapsed_sec: float = 0.0,
        model_id: str | None = None,
        provider: str = "",
        session_id: str = "",
        input_includes_cache_read: bool = True,
        input_includes_cache_write: bool = True,
        cost_usd: float | None = None,
        ordinary_input_tokens: int | None = None,
        prompt_cache_total_tokens: int | None = None,
    ) -> float | None:
        """
        Record token usage and elapsed time.

        Token counters are always retained. Monetary cost prefers the explicit
        provider value and falls back to cc's local model price table
        (modelCost.ts) when the provider reports none.

        Returns the request cost in USD, or ``None`` when the request is
        *unpriced*: the provider reported no cost and the local table does not
        cover the model. Callers must not treat ``None`` as ``$0``.
        """
        del provider
        # Provider payloads are untrusted transport data. Normalize counters
        # once at the accounting boundary so a malformed negative delta cannot
        # reduce session totals or make cache math appear billable.
        input_tokens = _nonnegative_int(input_tokens)
        output_tokens = _nonnegative_int(output_tokens)
        cache_creation_input_tokens = _nonnegative_int(cache_creation_input_tokens)
        cache_read_input_tokens = _nonnegative_int(cache_read_input_tokens)
        reasoning_output_tokens = _nonnegative_int(reasoning_output_tokens)
        elapsed_sec = _nonnegative_finite_float(elapsed_sec)
        if ordinary_input_tokens is None:
            base_input = max(
                0,
                input_tokens
                - (cache_read_input_tokens if input_includes_cache_read else 0)
                - (
                    cache_creation_input_tokens
                    if input_includes_cache_write
                    else 0
                ),
            )
        else:
            # Turn aggregation may combine providers with incompatible input
            # semantics. In that case the adapter/UsageInfo layer has already
            # normalized each request before summing; preserve that
            # authoritative ordinary-input total instead of trying to derive it
            # again from one lossy aggregate boolean.
            base_input = _nonnegative_int(ordinary_input_tokens)
        derived_prompt_cache_total = (
            base_input + cache_read_input_tokens + cache_creation_input_tokens
        )
        prompt_cache_total = (
            _nonnegative_int(prompt_cache_total_tokens)
            if prompt_cache_total_tokens is not None
            else derived_prompt_cache_total
        )
        cost: float | None = _nonnegative_finite_float(cost_usd) or None
        if cost is None:
            # cc computes cost locally from the model price table when the
            # provider does not report one. An unpriced model yields None.
            cost = _estimate_cost_usd(
                model_id,
                input_tokens=base_input,
                output_tokens=output_tokens,
                cache_creation_input_tokens=cache_creation_input_tokens,
                cache_read_input_tokens=cache_read_input_tokens,
            )

        self._add_usage(
            self.state,
            input_tokens=input_tokens,
            ordinary_input_tokens=base_input,
            output_tokens=output_tokens,
            cache_creation_input_tokens=cache_creation_input_tokens,
            cache_read_input_tokens=cache_read_input_tokens,
            prompt_cache_total=prompt_cache_total,
            reasoning_output_tokens=reasoning_output_tokens,
            cost=cost,
            elapsed_sec=elapsed_sec,
        )
        scoped_session_id = str(session_id or "").strip()
        if scoped_session_id:
            self._add_usage(
                self._session_states[scoped_session_id],
                input_tokens=input_tokens,
                ordinary_input_tokens=base_input,
                output_tokens=output_tokens,
                cache_creation_input_tokens=cache_creation_input_tokens,
                cache_read_input_tokens=cache_read_input_tokens,
                prompt_cache_total=prompt_cache_total,
                reasoning_output_tokens=reasoning_output_tokens,
                cost=cost,
                elapsed_sec=elapsed_sec,
            )

        return cost

    @staticmethod
    def _add_usage(state: CostTrackerState, **values: Any) -> None:
        state.input_tokens += int(values["input_tokens"])
        state.ordinary_input_tokens += int(values.get("ordinary_input_tokens", 0))
        state.output_tokens += int(values["output_tokens"])
        state.cache_creation_input_tokens += int(values["cache_creation_input_tokens"])
        state.cache_read_input_tokens += int(values["cache_read_input_tokens"])
        state.prompt_cache_total_tokens += max(0, int(values["prompt_cache_total"]))
        state.reasoning_output_tokens += int(values["reasoning_output_tokens"])
        cost = values["cost"]
        if cost is None:
            state.unpriced_requests += 1
        else:
            state.priced_requests += 1
            state.total_cost_usd += float(cost)
        state.total_duration_sec += float(values["elapsed_sec"])

    def get_summary(self, session_id: str = "") -> dict[str, Any]:
        clean_session_id = str(session_id or "").strip()
        state = self._session_states.get(clean_session_id, CostTrackerState()) if clean_session_id else self.state
        return {
            "scope": "session" if clean_session_id else "runtime",
            "session_id": clean_session_id or None,
            "input_tokens": state.input_tokens,
            "ordinary_input_tokens": state.ordinary_input_tokens,
            "output_tokens": state.output_tokens,
            "cache_creation_tokens": state.cache_creation_input_tokens,
            "cache_read_tokens": state.cache_read_input_tokens,
            "prompt_cache_total_tokens": state.prompt_cache_total_tokens,
            "reasoning_output_tokens": state.reasoning_output_tokens,
            "total_cost_usd": round(state.total_cost_usd, 4),
            # ``total_cost_usd`` only covers priced requests. With
            # ``cost_complete`` False it is a subtotal and must be shown as
            # partial/unpriced rather than as the session total.
            "priced_requests": state.priced_requests,
            "unpriced_requests": state.unpriced_requests,
            "cost_complete": state.unpriced_requests == 0,
            "total_duration_sec": round(state.total_duration_sec, 2),
            "uptime_sec": round(time.monotonic() - self._start_time, 2)
        }

    def reset(self) -> None:
        self.state = CostTrackerState()
        self._session_states.clear()
        self._start_time = time.monotonic()
