from __future__ import annotations

import asyncio

import pytest

from backend.agent.first_byte_waiter import ProviderStreamFailure, wait_for_provider_event


async def _single_event(delay: float = 0.0):
    if delay:
        await asyncio.sleep(delay)
    yield {"type": "first"}


async def _failed_stream():
    raise ValueError("provider transport failed")
    yield  # pragma: no cover


def test_wait_for_provider_event_returns_first_event() -> None:
    async def run():
        return await wait_for_provider_event(
            _single_event(0.01).__aiter__(),
            timeout_seconds=0.2,
            cancel_event=None,
            owner=set(),
        )

    event = asyncio.run(run())

    assert event == {"type": "first"}


def test_wait_for_provider_event_preserves_provider_exception_boundary() -> None:
    async def run():
        return await wait_for_provider_event(
            _failed_stream().__aiter__(),
            timeout_seconds=0.2,
            cancel_event=None,
            owner=set(),
        )

    with pytest.raises(ProviderStreamFailure) as caught:
        asyncio.run(run())

    assert isinstance(caught.value.cause, ValueError)


def test_wait_for_provider_event_enforces_timeout() -> None:
    async def run():
        return await wait_for_provider_event(
            _single_event(0.2).__aiter__(),
            timeout_seconds=0.01,
            cancel_event=None,
            owner=set(),
        )

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(run())
