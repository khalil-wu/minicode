"""Settled operations remain owned when permission revocation wins the race."""
from __future__ import annotations

import asyncio
import gc

import pytest

from backend.application_network import (
    ApplicationNetworkController, ApplicationNetworkDenied, DestinationPolicy,
)


def admitted():
    controller = ApplicationNetworkController()
    assert controller.publish(0, DestinationPolicy())
    return controller, controller.policy.acquire("https://fixture.invalid")


@pytest.mark.asyncio
@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("outcome", ["error", "cancelled", "success"])
async def test_revocation_keeps_authority_and_retrieves_same_tick_child_outcomes(nested, outcome):
    controller, permit = admitted()
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    unhandled = []
    loop.set_exception_handler(lambda _loop, context: unhandled.append(context))

    async def operation():
        controller.publish(0, DestinationPolicy(frozenset()))
        if outcome == "error":
            raise ValueError("operation failed concurrently with revocation")
        if outcome == "cancelled":
            raise asyncio.CancelledError()
        return "operation completed concurrently with revocation"

    async def nested_operation():
        return await permit.run(operation)

    async def rejected():
        with pytest.raises(ApplicationNetworkDenied, match="revoked"):
            await permit.run(nested_operation if nested else operation)

    try:
        await rejected()
        gc.collect()
        await asyncio.sleep(0)
        assert not unhandled
    finally:
        loop.set_exception_handler(previous)


@pytest.mark.asyncio
async def test_operation_error_is_preserved_while_its_permission_remains_valid():
    _controller, permit = admitted()
    original = ValueError("actual operation failure")

    async def operation():
        raise original

    with pytest.raises(ValueError) as caught:
        await permit.run(operation)
    assert caught.value is original


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["cancel", "revoke"])
async def test_pending_operation_finishes_cleanup_before_its_owner_returns(action):
    controller, permit = admitted()
    started, cleanup, blocked = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def operation():
        started.set()
        try:
            await blocked.wait()
        finally:
            cleanup.set()

    owner = asyncio.create_task(permit.run(operation))
    await started.wait()
    if action == "cancel":
        owner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await owner
    else:
        controller.publish(0, DestinationPolicy(frozenset()))
        with pytest.raises(ApplicationNetworkDenied, match="revoked"):
            await owner
    assert cleanup.is_set()
