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


@pytest.mark.parametrize("signal_cancel", [False, True], ids=["host-task", "cancel-signal"])
def test_cancellation_closes_the_owned_provider_read(signal_cancel: bool) -> None:
    async def run() -> None:
        entered = asyncio.Event()
        closed = asyncio.Event()
        cancel = asyncio.Event()

        async def provider():
            try:
                entered.set()
                await asyncio.Event().wait()
                yield "unreachable"
            finally:
                closed.set()

        stream = provider()
        owner = set()
        waiter = asyncio.create_task(wait_for_provider_event(
            stream, timeout_seconds=None, cancel_event=cancel, owner=owner,
        ))
        await entered.wait()
        if signal_cancel:
            cancel.set()
        else:
            waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert closed.is_set()
        assert not owner
        await stream.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("boundary", ["cancelled", "expired"])
def test_ended_wait_does_not_start_a_provider_request(boundary: str) -> None:
    async def run():
        calls = []
        cancel = asyncio.Event()
        if boundary == "cancelled":
            cancel.set()

        async def provider():
            calls.append("request sent")
            yield "response"

        stream = provider()
        try:
            error = asyncio.CancelledError if boundary == "cancelled" else asyncio.TimeoutError
            with pytest.raises(error):
                await wait_for_provider_event(stream, timeout_seconds=0 if boundary == "expired" else 1, cancel_event=cancel, owner=set())
            assert calls == []
        finally:
            await stream.aclose()

    asyncio.run(run())
