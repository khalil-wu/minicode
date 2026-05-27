import logging
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Basic Claude 3.5 Sonnet prices per 1M tokens (in USD)
# Default values can be overridden
DEFAULT_INPUT_PRICE_PER_M = 3.00
DEFAULT_OUTPUT_PRICE_PER_M = 15.00
DEFAULT_CACHE_WRITE_PRICE_PER_M = 3.75
DEFAULT_CACHE_READ_PRICE_PER_M = 0.30

MODEL_PRICES_PER_M: dict[str, tuple[float, float, float, float]] = {
    "claude-opus-4": (15.00, 75.00, 18.75, 1.50),
    "claude-sonnet-4": (3.00, 15.00, 3.75, 0.30),
    "claude-3-7-sonnet": (3.00, 15.00, 3.75, 0.30),
    "claude-3-5-sonnet": (3.00, 15.00, 3.75, 0.30),
    "claude-haiku": (0.80, 4.00, 1.00, 0.08),
    "gpt-5.4": (1.25, 10.00, 1.25, 0.125),
    "gpt-5.4-mini": (0.25, 2.00, 0.25, 0.025),
    "gpt-4o": (2.50, 10.00, 2.50, 0.25),
    "gpt-4.1": (2.00, 8.00, 2.00, 0.50),
}


def _prices_for_model(model_id: str | None) -> tuple[float, float, float, float]:
    normalized = (model_id or "").strip().lower()
    for prefix, prices in MODEL_PRICES_PER_M.items():
        if normalized.startswith(prefix):
            return prices
    return (
        DEFAULT_INPUT_PRICE_PER_M,
        DEFAULT_OUTPUT_PRICE_PER_M,
        DEFAULT_CACHE_WRITE_PRICE_PER_M,
        DEFAULT_CACHE_READ_PRICE_PER_M,
    )

@dataclass
class CostTrackerState:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
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
        elapsed_sec: float = 0.0,
        model_id: str | None = None,
    ) -> float:
        """
        Record token usage and elapsed time, update global totals, and return the cost for this turn.
        """
        # Exclude cached tokens if the API double-counts them as input_tokens
        # OpenAI/Anthropic APIs usually report input_tokens *including* cache tokens
        base_input = max(0, input_tokens - cache_read_input_tokens)
        input_price, output_price, cache_write_price, cache_read_price = _prices_for_model(model_id)

        cost = (
            (base_input / 1_000_000) * input_price +
            (output_tokens / 1_000_000) * output_price +
            (cache_creation_input_tokens / 1_000_000) * cache_write_price +
            (cache_read_input_tokens / 1_000_000) * cache_read_price
        )

        self.state.input_tokens += input_tokens
        self.state.output_tokens += output_tokens
        self.state.cache_creation_input_tokens += cache_creation_input_tokens
        self.state.cache_read_input_tokens += cache_read_input_tokens
        self.state.total_cost_usd += cost
        self.state.total_duration_sec += elapsed_sec

        return cost

    def get_summary(self) -> dict[str, Any]:
        return {
            "input_tokens": self.state.input_tokens,
            "output_tokens": self.state.output_tokens,
            "cache_creation_tokens": self.state.cache_creation_input_tokens,
            "cache_read_tokens": self.state.cache_read_input_tokens,
            "total_cost_usd": round(self.state.total_cost_usd, 4),
            "total_duration_sec": round(self.state.total_duration_sec, 2),
            "uptime_sec": round(time.monotonic() - self._start_time, 2)
        }

    def reset(self) -> None:
        self.state = CostTrackerState()
        self._start_time = time.monotonic()
