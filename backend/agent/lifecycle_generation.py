"""Conversation-scoped lifecycle generation ownership for MiniCode hosts."""

from __future__ import annotations

import asyncio
from typing import Any


_PUBLISHED_KEYS = (
    "runtime",
    "loader",
    "registry",
    "workspace_key",
    "fingerprint",
    "result",
    "host_actions_bound",
    "model_runtime",
    "model_registry",
)


class LifecycleGenerationState(dict[str, Any]):
    """Own one published generation and its deferred retirements.

    Publication and retirement mutations are centralized here. The state has
    one canonical runtime field; transport-era aliases are deliberately not
    accepted or published.
    """

    def __init__(self, owner_id: str) -> None:
        super().__init__()
        self.owner_id = str(owner_id or "").strip()
        self["lock"] = asyncio.Lock()

    @property
    def runtime(self) -> Any | None:
        return self.get("runtime")

    def is_current(self, runtime: Any) -> bool:
        return runtime is not None and self.runtime is runtime

    def publish(
        self,
        *,
        runtime: Any,
        loader: Any,
        registry: Any,
        workspace_key: str,
        fingerprint: str,
        result: Any,
        model_runtime: Any,
        model_registry: Any,
        host_actions_bound: bool = True,
    ) -> None:
        if self.get("shutting_down"):
            raise RuntimeError("cannot publish a lifecycle generation after shutdown")
        self.update(
            {
                "runtime": runtime,
                "loader": loader,
                "registry": registry,
                "workspace_key": str(workspace_key or ""),
                "fingerprint": str(fingerprint or ""),
                "result": result,
                "host_actions_bound": bool(host_actions_bound),
                "model_runtime": model_runtime,
                "model_registry": model_registry,
            }
        )

    def discard_published(self) -> None:
        for key in _PUBLISHED_KEYS:
            self.pop(key, None)

    def retire(
        self,
        *,
        runtime: Any,
        loader: Any,
        model_runtime: Any,
        reason: str,
        clear_loader_cache: bool,
        defer_until: asyncio.Task[Any] | None,
    ) -> None:
        self.setdefault("retired_generations", []).append(
            {
                "runtime": runtime,
                "loader": loader,
                "model_runtime": model_runtime,
                "reason": str(reason or "reload"),
                "clear_loader_cache": bool(clear_loader_cache),
                "defer_until": defer_until,
            }
        )

    def take_ready_retirements(self, *, force: bool = False) -> list[dict[str, Any]]:
        ready: list[dict[str, Any]] = []
        retained: list[dict[str, Any]] = []
        for record in list(self.get("retired_generations") or []):
            owner_task = record.get("defer_until")
            if force or not isinstance(owner_task, asyncio.Task) or owner_task.done():
                ready.append(record)
            else:
                retained.append(record)
        if retained:
            self["retired_generations"] = retained
        else:
            self.pop("retired_generations", None)
        return ready

    def fence_shutdown(self) -> None:
        self["shutting_down"] = True


__all__ = ["LifecycleGenerationState"]
