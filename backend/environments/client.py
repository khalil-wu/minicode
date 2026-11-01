"""One logical executor session; reconnect never replays a mutating RPC."""
from __future__ import annotations

import asyncio
import errno
from collections.abc import Awaitable, Callable
from typing import Any
from weakref import WeakSet

from websockets.exceptions import ConnectionClosed, InvalidStatus

from .config import EnvironmentSpec
from .errors import ExecutorClosed, ExecutorDisconnected, ExecutorProtocolError, ExecutorRpcError
from .rpc import RpcConnection
from .transport import TransportCloseReceipt, open_transport

SESSION_RECOVERY_TIMEOUT = 25.0
SESSION_RECOVERY_RETRY_INTERVAL = 0.1
ENVIRONMENT_PROBE_TIMEOUT = 10.0
ENVIRONMENT_INFO_TIMEOUT = 30.0


def _retryable(error: Exception) -> bool:
    retryable_io = isinstance(error, OSError) and error.errno in {
        errno.ECONNREFUSED, errno.ECONNRESET, errno.ECONNABORTED, errno.ENETDOWN, errno.ENETUNREACH,
        errno.EHOSTUNREACH, errno.EPIPE, errno.ENOTCONN, errno.ETIMEDOUT,
    }
    return retryable_io or isinstance(error, (TimeoutError, ConnectionClosed, ExecutorDisconnected)) or (
        isinstance(error, ExecutorRpcError) and error.code == -32010
    ) or (isinstance(error, InvalidStatus) and (error.response.status_code in {408, 409, 429}
                                              or 500 <= error.response.status_code < 600))


class ExecutorClient:
    def __init__(self, spec: EnvironmentSpec, *, client_name: str = "minicode-environment",
                 inbound_request: Callable[[str, Any], Awaitable[Any]] | None = None):
        self.spec = spec
        self.client_name = client_name
        self.inbound_request = inbound_request
        self.session_id: str | None = None
        self.environment_info: dict | None = None
        self.connection: RpcConnection | None = None
        self.connecting_connection: RpcConnection | None = None
        self.connect_task: asyncio.Task | None = None
        self.recovery_task: asyncio.Task | None = None
        self.close_task: asyncio.Task | None = None
        self.closed = False
        self.failure: Exception | None = None
        self.processes: dict[str, Any] = {}
        self.files: dict[str, Any] = {}
        self.cleanup_tasks: set[asyncio.Task] = set()
        self.cleanup_errors: list[Exception] = []
        self.resource_receipts: list[dict] = []
        self.transport_receipts: list[TransportCloseReceipt] = []
        self.on_idle: Callable[[], object] | None = None
        self.http_streams: dict[str, Any] = {}
        self.http_stream_handles = WeakSet()
        self.http_queued_bytes = 0
        self.next_http_request_id = 1

    def notify_idle(self) -> None:
        if self.on_idle is not None:
            self.on_idle()

    def retain_cleanup(self, operation: Awaitable[None]) -> None:
        task = asyncio.create_task(operation, name="executor-resource-cleanup")
        self.cleanup_tasks.add(task)

        def finished(done: asyncio.Task) -> None:
            self.cleanup_tasks.discard(done)
            if not done.cancelled() and (error := done.exception()) is not None:
                self.cleanup_errors.append(error)
            self.notify_idle()

        task.add_done_callback(finished)

    def _notification(self, rpc: RpcConnection, method: str, params: dict) -> None:
        if rpc is not self.connection and rpc is not self.connecting_connection:
            return
        if method == "http/request/bodyDelta":
            from .http import handle_http_body_delta
            handle_http_body_delta(self, rpc, params)
            return
        if method == "network/policyDecision":
            process = self.processes.get(params["processId"])
            if process is not None:
                process.network_audit.append(dict(params))
                if process.network_observer is not None:
                    process.network_observer({**params, "executionId": process.process_id,
                        "environmentId": self.spec.id})
            return
        if method in {"process/output", "process/exited", "process/closed"}:
            process = self.processes.get(params["processId"])
            if process is not None:
                try:
                    process.publish(method, params)
                except ExecutorProtocolError as error:
                    process.fail(error)

    async def _inbound(self, rpc: RpcConnection, method: str, params: dict):
        from .errors import ExecutorRpcError

        if rpc is not self.connection and rpc is not self.connecting_connection:
            raise ExecutorRpcError(-32000, "Executor request belongs to a retired transport")
        if method == "network/policyRequest":
            process = self.processes.get(params["processId"])
            if process is None or process.network_decider is None:
                raise ExecutorRpcError(-32000, "Executor process has no network policy decision owner")
            request = {**params["request"], "executionId": process.process_id, "environmentId": self.spec.id}
            return {"decision": await process.network_decider(request)}
        if self.inbound_request is None:
            raise ExecutorRpcError(-32601, f"method not found: {method}")
        return await self.inbound_request(method, params)

    def _disconnected(self, rpc: RpcConnection, error: Exception) -> None:
        if self.closed or rpc is not self.connection:
            return
        self.failure = error
        for route in tuple(self.http_streams.values()):
            route.fail_transport(error)
        if self.spec.url is not None and self.session_id and self.recovery_task is None:
            self.recovery_task = asyncio.create_task(self._recover(), name="executor-session-recovery")
        else:
            for process in tuple(self.processes.values()):
                process.fail(error)

    async def _open_initialized(self, resume_session_id: str | None) -> RpcConnection:
        launch = asyncio.create_task(open_transport(self.spec), name="executor-transport-open")
        try:
            transport = await asyncio.shield(launch)
        except asyncio.CancelledError:
            transport = await launch
            self.transport_receipts.append(await transport.close())
            raise
        rpc = RpcConnection(transport, on_notification=lambda method, params: self._notification(rpc, method, params),
                            on_disconnect=self._disconnected, inbound_request=lambda method, params: self._inbound(rpc, method, params))
        self.connecting_connection = rpc
        try:
            async with asyncio.timeout(self.spec.initialize_timeout_sec):
                response = await rpc.request("initialize", {"clientName": self.client_name, "resumeSessionId": resume_session_id})
                session_id = response["sessionId"]
                if not isinstance(session_id, str) or not session_id:
                    raise ExecutorProtocolError("exec-server initialize returned an invalid sessionId")
                if self.session_id is not None and session_id != self.session_id:
                    raise ExecutorProtocolError("exec-server initialized an unexpected session")
                await rpc.notify("initialized", {})
            # The session is installed only once the handshake completed. A
            # transport that fails before ``initialized`` leaves no session to
            # resume, so the next attempt starts a fresh one instead of
            # rejecting every later peer as an unexpected session.
            self.session_id = session_id
            if "environmentInfo" in response:
                self.environment_info = response["environmentInfo"]
            return rpc
        except BaseException:
            self.transport_receipts.append(await rpc.close())
            raise
        finally:
            if self.connecting_connection is rpc:
                self.connecting_connection = None

    async def _initial_connect(self) -> RpcConnection:
        try:
            rpc = await self._open_initialized(None)
            self.connection = rpc
            self.failure = None
            return rpc
        except Exception as error:
            self.failure = error
            raise

    async def _recover(self) -> None:
        old = self.connection
        try:
            if old is not None:
                self.transport_receipts.append(await old.close())
            async with asyncio.timeout(SESSION_RECOVERY_TIMEOUT):
                while True:
                    try:
                        rpc = await self._open_initialized(self.session_id)
                        self.connecting_connection = rpc
                        try:
                            for process in tuple(self.processes.values()):
                                if process.accepted:
                                    await process.recover(rpc)
                        except BaseException:
                            self.transport_receipts.append(await rpc.close())
                            raise
                        finally:
                            self.connecting_connection = None
                        self.connection = rpc
                        self.failure = None
                        return
                    except Exception as error:
                        if not _retryable(error):
                            raise
                        await asyncio.sleep(SESSION_RECOVERY_RETRY_INTERVAL)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.failure = error
            for process in tuple(self.processes.values()):
                process.fail(error)
        finally:
            self.recovery_task = None

    async def get_connection(self) -> RpcConnection:
        if self.closed:
            raise ExecutorClosed("executor is closed by its owner")
        if self.recovery_task is not None:
            await asyncio.shield(self.recovery_task)
        if self.connection is not None:
            if self.connection.connected:
                return self.connection
            raise self.failure or ExecutorDisconnected("exec-server transport disconnected")
        if self.connect_task is None or (self.connect_task.done() and self.failure is not None and _retryable(self.failure)):
            self.connect_task = asyncio.create_task(self._initial_connect(), name="executor-initialize")
        return await asyncio.shield(self.connect_task)

    async def request(self, method: str, params: Any, *, cleanup: bool = False) -> Any:
        if cleanup and self.closed:
            rpc = self.connection
            if rpc is None:
                raise ExecutorClosed("executor closed before cleanup could be confirmed")
        else:
            rpc = await self.get_connection()
        # A sent request belongs to this connection. Never retry it on another.
        try:
            return await rpc.request(method, params, cleanup=cleanup)
        finally:
            self.notify_idle()

    async def info(self) -> dict:
        rpc = await self.get_connection()
        try:
            async with asyncio.timeout(ENVIRONMENT_INFO_TIMEOUT):
                info = await rpc.request("environment/info", None)
        except TimeoutError as error:
            self.failure = error
            self.transport_receipts.append(await rpc.close())
            self._disconnected(rpc, ExecutorDisconnected("environment/info timed out"))
            raise
        self.environment_info = info
        self.notify_idle()
        return info

    async def status(self) -> dict:
        rpc = self.connection
        if rpc is None:
            return {"status": "disconnected", "error": str(self.failure)} if self.failure else {"status": "pending"}
        if not rpc.connected or self.recovery_task is not None:
            return {"status": "disconnected", "error": str(self.failure)} if self.failure else {"status": "pending"}
        try:
            async with asyncio.timeout(ENVIRONMENT_PROBE_TIMEOUT):
                response = await rpc.request("environment/status", None)
                if response["status"] != "ready":
                    raise ExecutorProtocolError("exec-server environment/status returned an unknown peer status")
            return {"status": "ready"}
        except Exception as error:
            self.failure = error
            if isinstance(error, TimeoutError):
                self.transport_receipts.append(await rpc.close())
            return {"status": "disconnected", "error": str(error)}

    async def _close(self) -> list[TransportCloseReceipt]:
        self.closed = True
        for stream in tuple(self.http_stream_handles):
            await stream.owner_close(ExecutorClosed("executor owner closed the HTTP response stream"))
        resources = (*tuple(self.processes.values()), *tuple(self.files.values()))
        startup = tuple(task for task in (self.connect_task, self.recovery_task) if task is not None and not task.done())
        for task in startup:
            task.cancel()
        await asyncio.gather(*startup, return_exceptions=True)
        if self.connection is not None:
            self.connection.abort_pending(ExecutorClosed("executor owner closed before the request was confirmed"))
        resource_results = await asyncio.gather(
            *(resource.aclose() for resource in resources),
            return_exceptions=True,
        )
        self.resource_receipts.extend(result for result in resource_results if isinstance(result, dict))
        self.cleanup_errors.extend(result for result in resource_results if isinstance(result, Exception))
        while self.cleanup_tasks:
            await asyncio.gather(*tuple(self.cleanup_tasks), return_exceptions=True)
        if self.connection is not None:
            self.transport_receipts.append(await self.connection.close())
        if self.cleanup_errors:
            raise ExceptionGroup("executor resource cleanup was not confirmed", self.cleanup_errors)
        return self.transport_receipts

    async def close(self) -> list[TransportCloseReceipt]:
        if self.close_task is None:
            self.close_task = asyncio.create_task(self._close(), name="executor-client-close")
        return await asyncio.shield(self.close_task)
