"""Timeout and cancellation policy for the official MCP session transport."""

import asyncio

import pytest

from mcp import types
from mcp.shared.exceptions import McpError

from backend.mcp.client import MCPClient, MCPTransport
from backend.async_cleanup import CleanupReceipt


def test_read_only_tool_timeout_is_not_retried() -> None:
    class _Session:
        def __init__(self) -> None:
            self.calls = 0

        async def call_tool(self, *_args, **_kwargs):
            self.calls += 1
            raise McpError(types.ErrorData(code=408, message="Timed out waiting for response"))

    session = _Session()
    client = MCPClient(
        "remote",
        transport=MCPTransport.HTTP,
        url="https://mcp.example/mcp",
    )
    client._connected = True
    client._session = session
    client._read_only_tools.add("search")

    result = asyncio.run(client.call_tool("search", {"query": "official SDK"}))

    assert result.is_error
    assert result.text == "Tool call timed out"
    assert session.calls == 1


def test_mutating_tool_timeout_is_not_retried() -> None:
    class _Session:
        def __init__(self) -> None:
            self.calls = 0

        async def call_tool(self, *_args, **_kwargs):
            self.calls += 1
            raise McpError(types.ErrorData(code=408, message="Timed out waiting for response"))

    session = _Session()
    client = MCPClient(
        "remote",
        transport=MCPTransport.HTTP,
        url="https://mcp.example/mcp",
    )
    client._connected = True
    client._session = session

    result = asyncio.run(client.call_tool("delete", {}))

    assert result.is_error
    assert result.text == "Tool call timed out"
    assert session.calls == 1


def test_tool_call_cancellation_propagates_to_the_sdk_request() -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    class _Session:
        async def call_tool(self, *_args, **_kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

    async def scenario() -> None:
        client = MCPClient(
            "remote",
            transport=MCPTransport.HTTP,
            url="https://mcp.example/mcp",
        )
        client._connected = True
        client._session = _Session()
        cancel_event = asyncio.Event()
        task = asyncio.create_task(client.call_tool(
            "search",
            {"query": "cancel"},
            request_owner={"cancel_event": cancel_event},
        ))
        await started.wait()
        cancel_event.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cancelled.is_set()

    asyncio.run(scenario())


def test_close_drains_active_sdk_tool_calls_before_transport_close() -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    class _Session:
        async def call_tool(self, *_args, **_kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

    async def scenario() -> None:
        client = MCPClient(
            "remote",
            transport=MCPTransport.HTTP,
            url="https://mcp.example/mcp",
        )
        client._connected = True
        client._session = _Session()
        call = asyncio.create_task(client.call_tool("wait", {}))
        await started.wait()

        assert await client.close()
        assert cancelled.is_set()
        with pytest.raises(asyncio.CancelledError):
            await call

    asyncio.run(scenario())


def test_close_exposes_pending_cleanup_when_request_misses_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def pending_receipt(*_args, **_kwargs) -> CleanupReceipt:
        return CleanupReceipt(
            requested=True,
            acknowledged=False,
            completed=False,
            timed_out=True,
            pending=1,
        )

    async def scenario() -> None:
        client = MCPClient(
            "remote",
            transport=MCPTransport.HTTP,
            url="https://mcp.example/mcp",
        )
        client._connected = True
        pending = asyncio.create_task(asyncio.Event().wait())
        client._active_request_tasks.add(pending)
        monkeypatch.setattr(
            "backend.mcp.client.cancel_and_drain_receipt",
            pending_receipt,
        )

        assert await client.close() is False
        assert client.cleanup_status["pending"] is True
        assert client.cleanup_status["reason"] == "active_requests_pending"
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)

    asyncio.run(scenario())
