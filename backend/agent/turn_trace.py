"""Opt-in per-turn latency tracing.

Enabled only when ``MINICODE_TURN_TRACE=1``.  Every call site that shares a key
(the conversation owning the turn) logs against one origin, so the marks form a
single ordered timeline: agent loop entry, iteration admission, per-iteration
preparation, provider dispatch, first provider event.
"""

from __future__ import annotations

import logging
import os
import time

logger = logging.getLogger("backend.agent.turn_trace")

ENABLED = os.getenv("MINICODE_TURN_TRACE") == "1"

_traces: dict[str, "TurnTrace"] = {}


class TurnTrace:
    def __init__(self, label: str) -> None:
        self.label = label
        self.started = time.monotonic()
        self.last = self.started

    def mark(self, name: str) -> None:
        if not ENABLED:
            return
        now = time.monotonic()
        step_ms = (now - self.last) * 1000.0
        total_ms = (now - self.started) * 1000.0
        self.last = now
        logger.info("turn_trace %s: %s +%.0fms (total %.0fms)", self.label, name, step_ms, total_ms)


def turn_trace_begin(label: str) -> TurnTrace:
    return TurnTrace(label)


def turn_trace_shared(key: str) -> TurnTrace:
    """Return the running trace for ``key``, creating it on first use."""

    if not ENABLED:
        return TurnTrace(key)
    trace = _traces.get(key)
    if trace is None:
        trace = TurnTrace(key)
        _traces[key] = trace
        # A trace is only useful for the turn that is running now.
        while len(_traces) > 16:
            _traces.pop(next(iter(_traces)), None)
    return trace


def turn_trace_reset(key: str) -> TurnTrace:
    """Begin a fresh timeline for a new turn, dropping the previous origin."""

    trace = TurnTrace(key)
    if ENABLED:
        _traces[key] = trace
    return trace
