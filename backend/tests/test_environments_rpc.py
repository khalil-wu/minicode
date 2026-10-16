"""Real WebSocket RPC boundary errors keep their original inbound owner."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import logging

import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from backend.environments.errors import ExecutorProtocolError, ExecutorRpcError
from backend.environments.rpc import RpcConnection
from backend.environments.transport import WebSocketTransport


@asynccontextmanager
async def connected_rpc(inbound):
    accepted = asyncio.Queue()
    stop = asyncio.Event()
    async def peer(websocket):
        await accepted.put(websocket)
        await stop.wait()
    async with serve(peer, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        async with connect(f"ws://127.0.0.1:{port}", proxy=None) as websocket:
            rpc = RpcConnection(WebSocketTransport(websocket), on_notification=lambda *_: None,
                on_disconnect=lambda *_: None, inbound_request=inbound)
            try:
                yield rpc, await accepted.get()
            finally:
                await rpc.close()
                stop.set()


async def call(peer, ident, method="network/policyRequest"):
    await peer.send(json.dumps({"id": ident, "method": method, "params": {"processId": "original-process"}}))
    async with asyncio.timeout(5):
        return json.loads(await peer.recv())


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [ExecutorProtocolError, RuntimeError, ValueError])
async def test_real_rpc_handler_failure_answers_same_id_and_keeps_original_cause(caplog, error_type):
    original = error_type("original native handler failed")
    async def inbound(method, _params):
        if method == "network/policyRequest":
            raise original
        return {"original-connection": "still usable"}
    async with connected_rpc(inbound) as (rpc, peer):
        response = await call(peer, "native-policy-original")
        assert response == {"id": "native-policy-original", "error": {
            "code": -32603, "message": str(original), "data": None}}
        record = next(record for record in caplog.records if "Executor inbound handler failed" in record.message)
        assert "native-policy-original" in record.message and "network/policyRequest" in record.message
        assert record.exc_info[1] is original
        assert rpc.connected
        assert await call(peer, 12, "still-usable") == {"id": 12, "result": {"original-connection": "still usable"}}


@pytest.mark.asyncio
async def test_real_rpc_keeps_explicit_error_code_data_and_request_id():
    async def inbound(_method, _params):
        raise ExecutorRpcError(-32004, "original process is gone", {"processId": "original-process"})
    async with connected_rpc(inbound) as (_rpc, peer):
        assert await call(peer, 41) == {"id": 41, "error": {"code": -32004,
            "message": "original process is gone", "data": {"processId": "original-process"}}}


@pytest.mark.asyncio
async def test_connected_request_cancellation_answers_without_cancelling_connection(caplog):
    entered = asyncio.Event()
    async def inbound(method, _params):
        if method == "after-cancel":
            return "same connection"
        entered.set()
        await asyncio.Event().wait()
    async with connected_rpc(inbound) as (rpc, peer):
        await peer.send(json.dumps({"id": "cancel-original", "method": "network/policyRequest", "params": {}}))
        await asyncio.wait_for(entered.wait(), 5)
        request = next(iter(rpc.inbound_tasks))
        request.cancel("original policy request stopped")
        async with asyncio.timeout(5):
            response = json.loads(await peer.recv())
        assert response == {"id": "cancel-original", "error": {"code": -32603,
            "message": "Executor inbound request was cancelled", "data": None}}
        await request
        assert not request.cancelled() and rpc.connected
        record = next(record for record in caplog.records if "Executor inbound request cancelled:" in record.message)
        assert "cancel-original" in record.message
        assert record.exc_info[1].args == ("original policy request stopped",)
        assert await call(peer, "after-original-cancel", "after-cancel") == {"id": "after-original-cancel", "result": "same connection"}


@pytest.mark.asyncio
async def test_connection_shutdown_cancels_original_waiter_without_fabricated_response(caplog):
    caplog.set_level(logging.DEBUG, logger="backend.environments.rpc")
    entered = asyncio.Event()
    async def inbound(_method, _params):
        entered.set()
        await asyncio.Event().wait()
    async with connected_rpc(inbound) as (rpc, peer):
        await peer.send(json.dumps({"id": "connection-close-original", "method": "network/policyRequest", "params": {}}))
        await asyncio.wait_for(entered.wait(), 5)
        request = next(iter(rpc.inbound_tasks))
        await rpc.close()
        assert request.cancelled()
        with pytest.raises(ConnectionClosed):
            await peer.recv()
        assert any("connection closed" in record.message and "connection-close-original" in record.message for record in caplog.records)
        assert not any("Executor inbound request cancelled:" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_response_send_failure_is_retrieved_with_original_owner_and_cause(caplog):
    original = OSError("original response pipe closed")
    rpc = RpcConnection.__new__(RpcConnection)
    rpc.inbound_tasks = set()
    async def failed_response():
        raise original
    task = asyncio.create_task(failed_response(), name="executor-inbound-request:network/policyRequest:original-id")
    rpc.inbound_tasks.add(task)
    with pytest.raises(OSError):
        await task
    rpc._inbound_finished(task)
    assert not rpc.inbound_tasks
    record = next(record for record in caplog.records if "Executor inbound response failed" in record.message)
    assert "network/policyRequest:original-id" in record.message and record.exc_info[1] is original
