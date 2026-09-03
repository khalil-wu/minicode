"""Shared markers used to carry interrupted-turn state across providers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


TURN_ABORTED_MARKER = "<turn_aborted>"


def contains_turn_aborted_marker(value: Any) -> bool:
    """Return whether a provider payload contains the interruption marker."""

    if isinstance(value, str):
        return TURN_ABORTED_MARKER in value
    if isinstance(value, Mapping):
        return any(contains_turn_aborted_marker(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(contains_turn_aborted_marker(item) for item in value)
    content = getattr(value, "content", None)
    if content is not None:
        return contains_turn_aborted_marker(content)
    return False
