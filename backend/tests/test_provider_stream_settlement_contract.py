from __future__ import annotations

import asyncio
from types import SimpleNamespace

from backend.agent.provider_stream_settlement import (
    ProviderStreamSettlement,
    settle_provider_stream,
)
from backend.llm.base import UsageInfo


class _Kernel:
    def __init__(self) -> None:
        self.closed: list[dict[str, object]] = []

    async def close_provider_attempt(self, _attempt, **kwargs) -> None:
        self.closed.append(kwargs)


class _Budget:
    def record_provider_usage_total(self, _usage) -> None:
        return None


class _Chain:
    def record_usage(self, **_kwargs) -> None:
        return None


def _settle(*, provider_done: bool):
    kernel = _Kernel()

    async def collect():
        updates = [
            item
            async for item in settle_provider_stream(
                retry_budget_boundary=None,
                budget_runtime=_Budget(),
                turn_kernel=kernel,
                provider_attempt=object(),
                finish_reason="stop" if provider_done else "",
                provider_stream_steered=False,
                rebuild_context_and_retry=False,
                state=SimpleNamespace(stopped_reason=""),
                pending_tool_calls=[],
                provider_raw_done={} if not provider_done else {"response_id": "r1"},
                provider_done=provider_done,
                visible_text_sanitizer=None,
                stream_state=SimpleNamespace(finish_reason=""),
                stream_text=SimpleNamespace(sanitize=lambda _scrub: None),
                context_builder=SimpleNamespace(record_actual_usage=lambda *_args, **_kwargs: None),
                usage=UsageInfo(),
                turn_usage=UsageInfo(),
                chain=_Chain(),
            )
        ]
        return updates, kernel.closed

    return asyncio.run(collect())


def test_eof_without_provider_done_closes_attempt_as_failed() -> None:
    updates, closed = _settle(provider_done=False)

    assert closed[0]["status"] == "failed"
    assert closed[0]["data"] == {
        "finish_reason": "stream_exhausted",
        "error_type": "provider_terminal_missing",
    }
    assert isinstance(updates[-1], ProviderStreamSettlement)
    settlement = updates[-1]
    assert settlement.action == "terminate"
    errors = [item for item in updates if getattr(item, "type", "") == "error"]
    assert len(errors) == 1
    assert errors[0].data["error_code"] == "provider_terminal_missing"


def test_provider_done_closes_attempt_as_completed() -> None:
    _, closed = _settle(provider_done=True)

    assert closed[0]["status"] == "completed"
    assert closed[0]["data"] == {"finish_reason": "stop"}
