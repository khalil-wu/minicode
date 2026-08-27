"""Provider-neutral lifecycle errors owned by the MiniCode harness core."""

from __future__ import annotations


class LifecycleStaleError(RuntimeError):
    """A captured lifecycle capability belongs to an obsolete generation."""


__all__ = ["LifecycleStaleError"]
