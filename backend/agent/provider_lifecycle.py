"""Provider wire lifecycle capability owned by the MiniCode harness."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol


class ProviderLifecycleRuntime(Protocol):
    """Optional hooks around one concrete provider request/response boundary."""

    async def emit_before_provider_request(self, payload: Any) -> Any: ...

    async def emit_before_provider_headers(
        self,
        headers: dict[str, Any],
    ) -> Any: ...

    async def emit_after_provider_response(
        self,
        status: int,
        headers: Mapping[str, Any],
    ) -> Any: ...


__all__ = ["ProviderLifecycleRuntime"]
