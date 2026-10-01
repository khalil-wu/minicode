"""Provider-neutral lifecycle observer boundary for the MiniCode harness."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
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
    run_context: RunContext = field(default_factory=RunContext)
    llm: Any = None
    _finish_task: asyncio.Task | None = field(default=None, init=False)

    @classmethod
    def create(
        cls,
        factory: LifecycleObserverFactory | None,
        llm: Any = None,
        **kwargs: Any,
    ) -> "LifecycleObserverOwner":
        return cls(
            factory(**kwargs) if factory is not None else None,
            run_context=kwargs["run_context"], llm=llm,
        )

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
        if self.observer is None or self._finish_task is not None:
            return None
        borrowers = {task for task in self.run_context.lifecycle_cleanup_tasks if not task.done()}

        async def finish_after_callbacks() -> None:
            while pending := {task for task in borrowers if not task.done()}:
                try:
                    await asyncio.wait(pending)
                except asyncio.CancelledError:
                    # End hooks cannot overtake a callback that still owns the observer.
                    continue
            await self.observer.finish(status=status, reason=reason)

        task = self._finish_task = asyncio.create_task(finish_after_callbacks())
        task.add_done_callback(lambda _done: setattr(self, "finished", True))
        try:
            done, _ = await asyncio.wait({task}, timeout=_OBSERVER_FINISH_TIMEOUT_SECONDS)
            if not done:
                await self.run_context.drain_lifecycle_task(
                    task, timeout=0, label="lifecycle observer finish", llm=self.llm,
                )
                return self._projection_error("Lifecycle observer finish deadline reached.", phase="finish")
            task.result()
        except asyncio.CancelledError:
            await self.run_context.drain_lifecycle_task(
                task, timeout=0, label="lifecycle observer finish", llm=self.llm,
            )
            return self._projection_error("Lifecycle observer finish interrupted after canonical terminal.", phase="finish")
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
