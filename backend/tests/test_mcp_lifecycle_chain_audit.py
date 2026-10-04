from __future__ import annotations

import asyncio

import pytest

from backend.mcp.client import MCPClient


@pytest.mark.asyncio
async def test_startup_timeout_retains_cancellation_resistant_lifecycle_for_late_cleanup(monkeypatch):
    import backend.mcp.client as module

    monkeypatch.setattr(module, "CANCELLATION_DRAIN_TIMEOUT_SECONDS", 0.03)
    release, entered = asyncio.Event(), asyncio.Event()
    client = MCPClient("audit-lifecycle", command="not-launched", startup_timeout=0.01)

    async def lifecycle(ready, close_event):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()

    monkeypatch.setattr(client, "_run_sdk_lifecycle", lifecycle)
    try:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(client.connect(), timeout=1.5)
        assert entered.is_set()
        assert client.cleanup_status["pending"]
        task = client._lifecycle_task
        assert task is not None and not task.done()
        with pytest.raises(RuntimeError, match="pending cleanup"):
            await client.connect()
        assert client._lifecycle_task is task
        reaper = asyncio.create_task(client.finish_pending_cleanup())
        await asyncio.sleep(0)
        assert not reaper.done()
        release.set()
        assert await asyncio.wait_for(reaper, timeout=1)
        assert client._lifecycle_task is None and not client.cleanup_status["pending"]
    finally:
        release.set()
        if client._lifecycle_task is not None:
            await asyncio.gather(client._lifecycle_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_close_consumes_a_cancelled_sdk_lifecycle_without_cancelling_its_caller():
    client = MCPClient("audit-closed", command="not-launched")
    task = asyncio.create_task(asyncio.sleep(100))
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    client._lifecycle_task = task
    assert await client.close()
    assert client._lifecycle_task is None
