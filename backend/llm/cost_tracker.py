import math
import time
from collections import defaultdict
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any

@dataclass
class CostTrackerState:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    prompt_cache_total_tokens: int = 0
    reasoning_output_tokens: int = 0
    total_cost_usd: float = 0.0
    total_duration_sec: float = 0.0

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

    _session_scope: ContextVar[str] = ContextVar("llm_cost_session_scope", default="")

    @classmethod
    def bind_session(cls, session_id: str) -> Token:
        return cls._session_scope.set(str(session_id or "").strip())

    @classmethod
    def unbind_session(cls, token: Token) -> None:
        cls._session_scope.reset(token)

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
        cost_usd: float | None = None,
    ) -> float:
        """
        Record token usage and elapsed time.

        Token counters are always retained. Monetary cost is recorded only when
        the provider/runtime supplies an explicit value; model names are not a
        pricing catalog and never select a local estimate.
        """
        del model_id, provider
        base_input = max(
            0,
            input_tokens - cache_read_input_tokens
            if input_includes_cache_read
            else input_tokens,
        )
        prompt_cache_total = base_input + cache_read_input_tokens + cache_creation_input_tokens
        explicit_cost = float(cost_usd) if cost_usd is not None else 0.0
        cost = max(0.0, explicit_cost) if math.isfinite(explicit_cost) else 0.0

        self._add_usage(
            self.state,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_input_tokens=cache_creation_input_tokens,
            cache_read_input_tokens=cache_read_input_tokens,
            prompt_cache_total=prompt_cache_total,
            reasoning_output_tokens=reasoning_output_tokens,
            cost=cost,
            elapsed_sec=elapsed_sec,
        )
        scoped_session_id = str(session_id or "").strip() or self._session_scope.get()
        if scoped_session_id:
            self._add_usage(
                self._session_states[scoped_session_id],
                input_tokens=input_tokens,
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
        state.output_tokens += int(values["output_tokens"])
        state.cache_creation_input_tokens += int(values["cache_creation_input_tokens"])
        state.cache_read_input_tokens += int(values["cache_read_input_tokens"])
        state.prompt_cache_total_tokens += max(0, int(values["prompt_cache_total"]))
        state.reasoning_output_tokens += int(values["reasoning_output_tokens"])
        state.total_cost_usd += float(values["cost"])
        state.total_duration_sec += float(values["elapsed_sec"])

    def get_summary(self, session_id: str = "") -> dict[str, Any]:
        clean_session_id = str(session_id or "").strip()
        state = self._session_states.get(clean_session_id, CostTrackerState()) if clean_session_id else self.state
        return {
            "scope": "session" if clean_session_id else "runtime",
            "session_id": clean_session_id or None,
            "input_tokens": state.input_tokens,
            "output_tokens": state.output_tokens,
            "cache_creation_tokens": state.cache_creation_input_tokens,
            "cache_read_tokens": state.cache_read_input_tokens,
            "prompt_cache_total_tokens": state.prompt_cache_total_tokens,
            "reasoning_output_tokens": state.reasoning_output_tokens,
            "total_cost_usd": round(state.total_cost_usd, 4),
            "total_duration_sec": round(state.total_duration_sec, 2),
            "uptime_sec": round(time.monotonic() - self._start_time, 2)
        }

    def reset(self) -> None:
        self.state = CostTrackerState()
        self._session_states.clear()
        self._start_time = time.monotonic()
