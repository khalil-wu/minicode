"""
Query Chain Tracking — correlate agent loop iterations with user turns.

Each user message starts a new chain with a unique ID. Every iteration within
that chain gets an incrementing depth counter. This enables:
- Log correlation across iterations
- Telemetry and debugging
- Performance analysis per user turn
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class QueryChainTracking:
    """Tracks the current query chain for correlation and telemetry."""
    chain_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    depth: int = 0
    source: str = "user"  # 'user' | 'compact' | 'recovery' | 'subagent'
    started_at: float = field(default_factory=time.time)
    user_message_preview: str = ""

    # Per-iteration metrics
    iteration_tokens_in: int = 0
    iteration_tokens_out: int = 0
    tool_calls_count: int = 0
    errors_count: int = 0

    def next_iteration(self) -> int:
        """Increment depth and return the new depth value."""
        self.depth += 1
        return self.depth

    def record_usage(self, input_tokens: int = 0, output_tokens: int = 0) -> None:
        """Record token usage for this iteration."""
        self.iteration_tokens_in += input_tokens
        self.iteration_tokens_out += output_tokens

    def record_tool_call(self) -> None:
        """Record a tool call."""
        self.tool_calls_count += 1

    def record_error(self) -> None:
        """Record an error."""
        self.errors_count += 1

    def reset_for_new_turn(self, user_message: str = "", source: str = "user") -> None:
        """Start a new chain for a new user message."""
        self.chain_id = uuid.uuid4().hex[:12]
        self.depth = 0
        self.source = source
        self.started_at = time.time()
        self.user_message_preview = user_message[:100]
        self.iteration_tokens_in = 0
        self.iteration_tokens_out = 0
        self.tool_calls_count = 0
        self.errors_count = 0

    @property
    def elapsed_seconds(self) -> float:
        return time.time() - self.started_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "chain_id": self.chain_id,
            "depth": self.depth,
            "source": self.source,
            "elapsed_seconds": round(self.elapsed_seconds, 2),
            "tokens_in": self.iteration_tokens_in,
            "tokens_out": self.iteration_tokens_out,
            "tool_calls": self.tool_calls_count,
            "errors": self.errors_count,
            "user_preview": self.user_message_preview,
        }

    def to_log_context(self) -> str:
        """Short string for log messages: [chain:abc123 d=3]"""
        return f"[chain:{self.chain_id} d={self.depth}]"
