"""Small, non-semantic helpers used by the provider loop.

This module deliberately contains no user-intent classification, answer
quality gate, or natural-language retry policy.  The agent must be free to
choose whether to continue, answer partially, or report a blocker.  Only
protocol bookkeeping belongs here.
"""

from __future__ import annotations

import inspect
from typing import Any

from backend.agent.context import ContextBuilder


def append_assistant_history(
    ctx: ContextBuilder,
    content: str,
    *,
    phase: str = "",
    provider_items: list[dict[str, Any]] | None = None,
) -> None:
    """Append assistant history while tolerating legacy context fakes."""

    append_assistant = ctx.append_assistant
    try:
        parameters = inspect.signature(append_assistant).parameters
    except (TypeError, ValueError):
        parameters = {}
    accepts_kwargs = any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )
    kwargs: dict[str, Any] = {}
    if accepts_kwargs or "phase" in parameters:
        kwargs["phase"] = phase
    if accepts_kwargs or "provider_items" in parameters:
        kwargs["provider_items"] = provider_items
    append_assistant(content, **kwargs)
