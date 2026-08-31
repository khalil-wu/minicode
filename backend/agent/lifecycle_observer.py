"""Provider-neutral lifecycle observer boundary for the MiniCode harness."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from backend.agent.message import AgentEvent
from backend.agent.run_context import RunContext


logger = logging.getLogger(__name__)

_OBSERVER_FINISH_TIMEOUT_SECONDS = 5.0


class LifecycleObserver(Protocol):
    async def start(self) -> None: ...
    async def observe(self, event: Any) -> None: ...
    async def finish(self, *, status: str = "completed", reason: str = "") -> None: ...


LifecycleObserverFactory = Callable[..., LifecycleObserver | None]


def resolve_lifecycle_runtime(
    *,
    session_context: Any | None = None,
    run_context: RunContext | None = None,
) -> Any | None:
    """Resolve the session-owned lifecycle capability from canonical owners."""
    if run_context is not None and run_context.lifecycle_runtime is not None:
        return run_context.lifecycle_runtime
    if session_context is not None:
        runtime = getattr(session_context, "lifecycle_runtime", None)
        if runtime is not None:
            return runtime
    return None


@dataclass(slots=True)
class LifecycleObserverOwner:
    """Own one query's optional lifecycle projection and its failure events."""

    observer: LifecycleObserver | None = None
    finished: bool = False

    @classmethod
    def create(
        cls,
        factory: LifecycleObserverFactory | None,
        **kwargs: Any,
    ) -> "LifecycleObserverOwner":
        return cls(factory(**kwargs) if factory is not None else None)

    async def start(self) -> AgentEvent | None:
        if self.observer is None:
            return None
        try:
            await self.observer.start()
        except Exception as exc:
            logger.warning(
                "Lifecycle observer start failed; continuing canonical run",
                exc_info=True,
            )
            return self._projection_error(
                f"Lifecycle observer start failed: {exc}",
                phase="start",
            )
        return None

    async def observe(self, event: AgentEvent) -> AgentEvent | None:
        if self.observer is None:
            return None
        try:
            await self.observer.observe(event)
        except Exception as exc:
            logger.warning(
                "Lifecycle observer event failed; continuing canonical run",
                exc_info=True,
            )
            return self._projection_error(
                f"Lifecycle observer event projection failed: {exc}",
                phase="observe",
            )
        return None

    async def finish(self, *, status: str, reason: str) -> AgentEvent | None:
        if self.observer is None or self.finished:
            return None
        self.finished = True
        try:
            await asyncio.wait_for(
                self.observer.finish(status=status, reason=reason),
                timeout=_OBSERVER_FINISH_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            logger.warning(
                "Lifecycle observer finish failed after canonical terminal",
                exc_info=True,
            )
            return self._projection_error(
                f"Lifecycle observer finish failed: {exc}",
                phase="finish",
            )
        return None

    @staticmethod
    def _projection_error(message: str, *, phase: str) -> AgentEvent:
        event = AgentEvent.error(
            message,
            recoverable=False,
            error_type="projection",
            error_code=f"lifecycle_observer.{phase}_failed",
        )
        event.data["projection_phase"] = phase
        return event


__all__ = [
    "LifecycleObserver",
    "LifecycleObserverFactory",
    "LifecycleObserverOwner",
    "resolve_lifecycle_runtime",
]
