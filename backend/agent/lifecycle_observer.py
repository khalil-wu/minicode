"""Provider-neutral lifecycle observer boundary for the MiniCode harness."""

from __future__ import annotations

from collections.abc import Callable, Mapping, MutableMapping
from typing import Any, Protocol


class LifecycleObserver(Protocol):
    async def start(self) -> None: ...
    async def observe(self, event: Any) -> None: ...
    async def finish(self, *, status: str = "completed", reason: str = "") -> None: ...


LifecycleObserverFactory = Callable[..., LifecycleObserver | None]

LIFECYCLE_RUNTIME_METADATA_KEY = "_lifecycle_runtime"


def resolve_lifecycle_runtime(
    metadata: Mapping[str, Any] | None = None,
    *,
    session_context: Any | None = None,
) -> Any | None:
    """Resolve the session-owned lifecycle capability from canonical owners."""
    if session_context is not None:
        runtime = getattr(session_context, "lifecycle_runtime", None)
        if runtime is not None:
            return runtime
    source = metadata if isinstance(metadata, Mapping) else {}
    runtime = source.get(LIFECYCLE_RUNTIME_METADATA_KEY)
    return runtime if runtime is not None else None


def install_lifecycle_runtime(
    metadata: MutableMapping[str, Any],
    runtime: Any | None,
) -> Any | None:
    """Install the canonical capability without adding legacy metadata keys."""
    if runtime is None:
        metadata.pop(LIFECYCLE_RUNTIME_METADATA_KEY, None)
    else:
        metadata[LIFECYCLE_RUNTIME_METADATA_KEY] = runtime
    return runtime


class NullLifecycleObserver:
    """Provider-neutral observer used when the host installs no projection."""

    async def start(self) -> None:
        return None

    async def observe(self, event: Any) -> None:
        del event

    async def finish(self, *, status: str = "completed", reason: str = "") -> None:
        del status, reason


def null_lifecycle_observer_factory(**_kwargs: Any) -> LifecycleObserver:
    return NullLifecycleObserver()


__all__ = [
    "LifecycleObserver",
    "LifecycleObserverFactory",
    "LIFECYCLE_RUNTIME_METADATA_KEY",
    "NullLifecycleObserver",
    "install_lifecycle_runtime",
    "null_lifecycle_observer_factory",
    "resolve_lifecycle_runtime",
]
