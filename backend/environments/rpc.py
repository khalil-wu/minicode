"""The Codex exec-server JSON-RPC dialect (no jsonrpc field)."""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from .errors import ExecutorClosed, ExecutorDisconnected, ExecutorProtocolError, ExecutorRpcError
from .transport import MAX_MESSAGE_BYTES, TransportCloseReceipt

MAX_IN_FLIGHT_CALLS = 1024
MAX_JSON_NODES = 256 * 1024
STREAM_NOTIFICATION_METHODS = {"process/output", "process/exited", "process/closed", "http/request/bodyDelta"}
logger = logging.getLogger(__name__)


class RpcConnection:
    def __init__(self, transport, *, on_notification: Callable[[str, dict], None],
                 on_disconnect: Callable[[RpcConnection, Exception], None],
                 inbound_request: Callable[[str, Any], Awaitable[Any]] | None = None):
        self.transport = transport
        self.on_notification = on_notification
        self.on_disconnect = on_disconnect
        self.inbound_request = inbound_request
        self.pending: dict[int, asyncio.Future] = {}
        self.cleanup_request_ids: set[int] = set()
        self.next_id = 1
        self.send_lock = asyncio.Lock()
        self.failure: Exception | None = None
        self.closed = False
        self.inbound_tasks: set[asyncio.Task] = set()
        self.reader = asyncio.create_task(self._read(), name="executor-rpc-reader")
        self.close_task: asyncio.Task[TransportCloseReceipt] | None = None

    @property
    def connected(self) -> bool:
        return not self.closed and self.failure is None

    async def send(self, message: dict) -> None:
        async with self.send_lock:
            if not self.connected:
                raise self.failure or ExecutorClosed("exec-server connection is closed")
            await self.transport.send(json.dumps(message, ensure_ascii=False, allow_nan=False, separators=(",", ":")))

    async def request(self, method: str, params: Any, *, cleanup: bool = False) -> Any:
        if len(self.pending) >= MAX_IN_FLIGHT_CALLS + int(cleanup):
            raise ExecutorProtocolError("exec-server in-flight request limit reached")
        request_id = self.next_id
        self.next_id += 1
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        if cleanup:
            self.cleanup_request_ids.add(request_id)
        try:
            await self.send({"id": request_id, "method": method, "params": params})
            return await future
        finally:
            self.pending.pop(request_id, None)
            self.cleanup_request_ids.discard(request_id)
            if not future.done():
                future.cancel()

    async def notify(self, method: str, params: Any) -> None:
        await self.send({"method": method, "params": params})

    def abort_pending(self, reason: Exception) -> None:
        # Retire ordinary waiters before host teardown. The reader/transport
        # remains available for the original resource cleanup requests.
        for request_id, future in tuple(self.pending.items()):
            if request_id not in self.cleanup_request_ids and not future.done():
                future.set_exception(reason)

    async def _handle_request(self, message: dict) -> None:
        try:
            if self.inbound_request is None:
                raise ExecutorRpcError(-32601, f"method not found: {message['method']}")
            result = await self.inbound_request(message["method"], message.get("params"))
        except ExecutorRpcError as error:
            response = {"id": message["id"], "error": {"code": error.code, "message": str(error), "data": error.data}}
        except asyncio.CancelledError:
            if not self.connected:
                logger.debug("Executor inbound request cancelled after connection closed: id=%r method=%s",
                    message["id"], message["method"], exc_info=True)
                raise
            logger.warning("Executor inbound request cancelled: id=%r method=%s", message["id"], message["method"], exc_info=True)
            response = {"id": message["id"], "error": {"code": -32603,
                "message": "Executor inbound request was cancelled", "data": None}}
        except Exception as error:
            # Source rpc.rs::internal_error terminates the original request;
            # keep the Python cause and owner in the log as well as the wire.
            logger.exception("Executor inbound handler failed: id=%r method=%s", message["id"], message["method"])
            response = {"id": message["id"], "error": {"code": -32603, "message": str(error), "data": None}}
        else:
            response = {"id": message["id"], "result": result}
        if self.connected:
            await self.send(response)

    def _inbound_finished(self, task: asyncio.Task) -> None:
        self.inbound_tasks.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.error("Executor inbound response failed: owner=%s", task.get_name(),
                exc_info=(type(error), error, error.__traceback__))

    async def _read(self) -> None:
        try:
            while True:
                raw = await self.transport.recv()
                size = len(raw.encode("utf-8"))
                if size > MAX_MESSAGE_BYTES:
                    raise ExecutorProtocolError("exec-server message exceeds 64 MiB")
                message = json.loads(raw)
                if not isinstance(message, dict):
                    raise ExecutorProtocolError("exec-server JSON-RPC message must be an object")
                nodes, stack = 0, [message]
                while stack:
                    item = stack.pop()
                    nodes += 1
                    if nodes > MAX_JSON_NODES:
                        raise ExecutorProtocolError("exec-server JSON-RPC message exceeds 262144 JSON values")
                    if isinstance(item, dict):
                        stack.extend(item.values())
                    elif isinstance(item, list):
                        stack.extend(item)
                if "method" in message:
                    limit = 2 * 1024 * 1024 if message["method"] in STREAM_NOTIFICATION_METHODS and "id" not in message else 8192
                    if size > limit:
                        raise ExecutorProtocolError("exec-server inbound request/notification exceeds Source limit")
                    if "id" in message:
                        task = asyncio.create_task(self._handle_request(message),
                            name=f"executor-inbound-request:{message['method']}:{message['id']}")
                        self.inbound_tasks.add(task)
                        task.add_done_callback(self._inbound_finished)
                    else:
                        self.on_notification(message["method"], message.get("params", {}))
                else:
                    future = self.pending.get(message["id"])
                    if future is None or future.done():
                        continue
                    if "error" in message:
                        error = message["error"]
                        future.set_exception(ExecutorRpcError(error["code"], error["message"], error.get("data")))
                    else:
                        future.set_result(message["result"])
        except asyncio.CancelledError:
            raise
        except Exception as error:
            failure = error if isinstance(error, ExecutorProtocolError) else (
                ExecutorProtocolError(str(error)) if isinstance(error, (ValueError, KeyError, TypeError, UnicodeError))
                else ExecutorDisconnected(str(error))
            )
            self.failure = failure
            for future in tuple(self.pending.values()):
                if not future.done():
                    future.set_exception(failure)
            self.on_disconnect(self, failure)

    async def _close(self) -> TransportCloseReceipt:
        self.closed = True
        failure = ExecutorClosed("exec-server connection closed by owner")
        for future in tuple(self.pending.values()):
            if not future.done():
                future.set_exception(failure)
        for task in self.inbound_tasks:
            task.cancel()
        await asyncio.gather(*self.inbound_tasks, return_exceptions=True)
        receipt = await self.transport.close()
        self.reader.cancel()
        await asyncio.gather(self.reader, return_exceptions=True)
        return receipt

    async def close(self) -> TransportCloseReceipt:
        if self.close_task is None:
            self.close_task = asyncio.create_task(self._close(), name="executor-rpc-close")
        return await asyncio.shield(self.close_task)
