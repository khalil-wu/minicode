"""Owned RPC transport to the fixed Source network-policy/proxy runtime."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
import json
import subprocess
import sys
from typing import Any

from backend.environments.config import EnvironmentSpec
from backend.environments.errors import ExecutorClosed, ExecutorProtocolError, ExecutorRpcError
from backend.environments.rpc import RpcConnection
from backend.environments.transport import open_transport
from backend.permissions.native_execpolicy import packaged_execpolicy_directory


SOURCE_COMMIT = "970111f6cb62cc02fe36a82ea7d71b37a2889c97"
NetworkDecider = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


def packaged_network_directory() -> Path:
    return Path(__file__).with_name("resources") / packaged_execpolicy_directory().name


class _NetworkExecution:
    def __init__(self, runtime, execution_id: str, prepared: dict, *, remote: bool, observer):
        self.runtime = runtime
        self.execution_id = execution_id
        self.remote = remote
        if remote:
            self.remote_launch = prepared["remoteLaunch"]
        else:
            self.env = prepared["env"]
            self.sandbox_context = prepared["sandboxContext"]
            self.restricting_sid = prepared["restrictingSid"]
            self.provisioning_settings = prepared["provisioningSettings"]
        self.network_audit: list[dict] = []
        self.observer = observer
        self.borrowers = 1
        self.close_task: asyncio.Task | None = None

    def record(self, audit: dict) -> None:
        self.network_audit.append(audit)
        if self.observer is not None:
            self.observer(audit)

    async def _close(self) -> None:
        try:
            connection = await self.runtime.connection()
            await connection.request("network/release", {"executionId": self.execution_id}, cleanup=True)
        finally:
            self.runtime.leases.pop(self.execution_id)
            self.runtime.deciders.pop(self.execution_id, None)

    async def aclose(self) -> None:
        if self.close_task is None:
            self.close_task = asyncio.create_task(self._close(), name=f"network-execution-close-{self.execution_id}")
        await asyncio.shield(self.close_task)


class PreparedNetworkLease:
    """A borrower of one actual native execution, never an ID lookup."""

    def __init__(self, execution: _NetworkExecution):
        self._execution = execution
        self.runtime = execution.runtime
        self.execution_id = execution.execution_id
        self.network_audit = execution.network_audit
        if execution.remote:
            self.remote_launch = execution.remote_launch
        else:
            self.env = execution.env
            self.sandbox_context = execution.sandbox_context
            self.restricting_sid = execution.restricting_sid
            self.provisioning_settings = execution.provisioning_settings
        self.closed = False

    def clone(self) -> PreparedNetworkLease:
        if self.closed or self._execution.close_task is not None:
            raise ExecutorClosed("Native network execution lease is closed")
        self._execution.borrowers += 1
        return PreparedNetworkLease(self._execution)

    async def decide(self, request: dict) -> dict:
        connection = await self.runtime.connection()
        return await connection.request("network/decideRemote", {"executionId": self.execution_id, "request": request})

    async def apply_amendment(self, host: str, protocol: str, action: str) -> dict:
        connection = await self.runtime.connection()
        return await connection.request("network/applyAmendment", {"executionId": self.execution_id,
            "host": host, "protocol": protocol, "action": action})

    async def flush_audit(self) -> None:
        connection = await self.runtime.connection()
        await connection.request("network/flushAudit", {"executionId": self.execution_id}, cleanup=True)

    async def aclose(self) -> None:
        if not self.closed:
            self.closed = True
            self._execution.borrowers -= 1
        if self._execution.borrowers == 0:
            await self._execution.aclose()


class NativeNetworkRuntime:
    """One native proxy service, with Source's shared ingress and real routes."""

    def __init__(self, directory: Path | None = None):
        self.directory = Path(directory) if directory is not None else packaged_network_directory()
        self.binary = self.directory / ("minicode-network.exe" if sys.platform == "win32" else "minicode-network")
        self.connect_task: asyncio.Task | None = None
        self.close_task: asyncio.Task | None = None
        self.rpc: RpcConnection | None = None
        self.failure: Exception | None = None
        self.closed = False
        self.leases: dict[str, _NetworkExecution] = {}
        self.deciders: dict[str, NetworkDecider] = {}
        self.pending_cleanup: set[asyncio.Task] = set()
        self.cleanup_errors: list[Exception] = []
        self.notifications: list[tuple[str, dict]] = []
        self.local_prerequisite = None

    async def _inbound(self, method: str, params: dict) -> dict:
        if method != "network/policyRequest":
            raise ExecutorRpcError(-32601, f"Unknown native network request `{method}`")
        decider = self.deciders.get(params["executionId"])
        if decider is None:
            raise ExecutorRpcError(-32000, "Network execution no longer has a policy decision owner")
        return await decider(params)

    def _notification(self, method: str, params: dict) -> None:
        if method == "network/policyDecision":
            execution = self.leases.get(params["executionId"])
            if execution is not None:
                execution.record(dict(params))
        else:
            self.notifications.append((method, params))

    def _disconnect(self, _connection, error: Exception) -> None:
        self.failure = error

    async def _connect(self) -> RpcConnection:
        transport = await open_transport(EnvironmentSpec("minicode-network-runtime", program=str(self.binary),
            cwd=self.directory))
        rpc = RpcConnection(transport, on_notification=self._notification, on_disconnect=self._disconnect,
            inbound_request=self._inbound)
        self.rpc = rpc
        try:
            identity = await rpc.request("identity", {})
            if identity != {"identity": "minicode-network", "protocol": 1, "sourceCommit": SOURCE_COMMIT}:
                raise ExecutorProtocolError("Native network runtime does not match the pinned Source identity")
            return rpc
        except BaseException:
            await rpc.close()
            raise

    async def connection(self) -> RpcConnection:
        if self.closed:
            raise ExecutorClosed("Native network runtime is closed")
        if self.failure is not None:
            raise self.failure
        if self.connect_task is None:
            self.connect_task = asyncio.create_task(self._connect(), name="source-network-connect")
        return await asyncio.shield(self.connect_task)

    async def inspect(self, policy: dict) -> dict:
        connection = await self.connection()
        return await connection.request("network/inspect", policy)

    def invoke_sync(self, method: str, params: dict) -> dict:
        result = subprocess.run([str(self.binary)], cwd=self.directory,
            input=(json.dumps({"id": 1, "method": method, "params": params}, ensure_ascii=False) + "\n").encode("utf-8"),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0, check=True)
        response = json.loads(result.stdout)
        if "error" in response:
            error = response["error"]
            raise ExecutorRpcError(error["code"], error["message"], error.get("data"))
        return response["result"]

    def inspect_sync(self, policy: dict) -> dict:
        return self.invoke_sync("network/inspect", policy)

    def append_network_amendment(self, *, path: Path, host: str, protocol: str, action: str) -> dict:
        return self.invoke_sync("network/appendAmendment", {"path": str(path), "host": host,
            "protocol": protocol, "action": action})

    async def _prepare(self, method: str, policy: dict, env: dict[str, str], *, environment_id: str,
            execution_id: str, attribution_token: str, decider: NetworkDecider | None, remote: bool,
            observer=None) -> PreparedNetworkLease:
        connection = await self.connection()
        if decider is not None:
            self.deciders[execution_id] = decider
        operation = asyncio.create_task(connection.request(method, {"policy": policy,
            "env": env, "environmentId": environment_id, "executionId": execution_id,
            "attributionToken": attribution_token, "enableApprovalFlow": decider is not None}),
            name=f"source-network-prepare-{execution_id}")
        try:
            prepared = await asyncio.shield(operation)
        except asyncio.CancelledError:
            # The actual native request still owns a possible route. Settle
            # and close it rather than losing it with the cancelled borrower.
            async def close_after_prepare():
                try:
                    result = await operation
                except BaseException:
                    self.deciders.pop(execution_id, None)
                    raise
                execution = _NetworkExecution(self, execution_id, result, remote=remote, observer=observer)
                self.leases[execution_id] = execution
                await execution.aclose()

            cleanup = asyncio.create_task(close_after_prepare(), name=f"cancelled-network-prepare-{execution_id}")
            self.pending_cleanup.add(cleanup)

            def settled(done):
                self.pending_cleanup.discard(done)
                if not done.cancelled() and (error := done.exception()) is not None:
                    self.cleanup_errors.append(error)

            cleanup.add_done_callback(settled)
            raise
        except BaseException:
            self.deciders.pop(execution_id, None)
            raise
        execution = _NetworkExecution(self, execution_id, prepared, remote=remote, observer=observer)
        self.leases[execution_id] = execution
        return PreparedNetworkLease(execution)

    async def prepare_local(self, policy: dict, env: dict[str, str], *, environment_id: str,
            execution_id: str, attribution_token: str, decider: NetworkDecider | None = None,
            observer=None) -> PreparedNetworkLease:
        return await self._prepare("network/prepareLocal", policy, env, environment_id=environment_id,
            execution_id=execution_id, attribution_token=attribution_token, decider=decider, remote=False, observer=observer)

    async def prepare_remote(self, policy: dict, *, environment_id: str, execution_id: str,
            attribution_token: str, decider: NetworkDecider | None = None, observer=None) -> PreparedNetworkLease:
        return await self._prepare("network/prepareRemote", policy, {}, environment_id=environment_id,
            execution_id=execution_id, attribution_token=attribution_token, decider=decider, remote=True, observer=observer)

    async def _close(self) -> None:
        recorded_errors = tuple(self.cleanup_errors)
        results = await asyncio.gather(*(lease.aclose() for lease in tuple(self.leases.values())),
            *tuple(self.pending_cleanup), return_exceptions=True)
        results.extend(recorded_errors)
        if self.connect_task is not None:
            await asyncio.gather(asyncio.shield(self.connect_task), return_exceptions=True)
        if self.rpc is not None:
            receipt = await self.rpc.close()
            if receipt.exit_code != 0 or receipt.forced:
                results.append(RuntimeError(f"Native network runtime did not close normally: {receipt}"))
        self.closed = True
        errors = [result for result in results if isinstance(result, Exception)]
        if errors:
            raise ExceptionGroup("Native network ownership did not close", errors)

    async def aclose(self) -> None:
        if self.close_task is None:
            self.close_task = asyncio.create_task(self._close(), name="source-network-runtime-close")
        await asyncio.shield(self.close_task)
