"""Resolve an optional caller-owned turn budget.

The loop must not decide that a task is "simple" or that a tool result counts
as progress based on text/signatures.  Callers own one concrete iteration
budget. Zero leaves the model-driven loop open-ended.
"""

from __future__ import annotations

from backend.config import AgentSettings


def resolve_turn_max_iterations(settings: AgentSettings) -> int:
    """Return only the caller-configured cap; zero means unlimited."""
    return max(0, int(settings.max_iterations or 0))
