"""Original-session process handles, ordered ByteChunk output and real close."""
from __future__ import annotations

import asyncio
import base64
from copy import deepcopy
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from .errors import ExecutorClosed, ExecutorDisconnected, ExecutorProtocolError, ExecutorRpcError
from .events import ProcessEventLog, event_bytes
from .filesystem import decode_bytes, file_uri


@dataclass(frozen=True)
class ProcessOutputChunk:
    seq: int
    stream: str
    chunk: bytes


@dataclass(frozen=True)
class ProcessRead:
    chunks: tuple[ProcessOutputChunk, ...]
    next_seq: int
    exited: bool
    exit_code: int | None
    closed: bool
    failure: str | None
    sandbox_denied: bool = False

    @classmethod
    def from_wire(cls, data: dict) -> ProcessRead:
        return cls(tuple(ProcessOutputChunk(item["seq"], item["stream"], decode_bytes(item["chunk"], "chunk"))
                         for item in data["chunks"]), data["nextSeq"], data["exited"], data["exitCode"],
                   data["closed"], data["failure"], data.get("sandboxDenied", False))


class ExecutorProcess:
    def __init__(self, client, process_id: str, sandbox: dict | None):
        self.client = client
        self.process_id = process_id
        self.sandbox = sandbox
        self.sandbox_type: str | None = None
        self.accepted = False
        self.start_task: asyncio.Task | None = None
        self.last_seq = 0
        self.pending_events: dict[int, dict] = {}
        self.events = ProcessEventLog()
        self.default_receiver = None
        self.exited = False
        self.exit_code: int | None = None
        self.closed = False
        self.failure: Exception | None = None
        self.released = False
        self.close_task: asyncio.Task | None = None
        self.close_failure: Exception | None = None
        self.network_decider = None
        self.network_observer = None
        self.network_audit: list[dict] = []

    def _publish_ready(self) -> None:
        while self.last_seq + 1 in self.pending_events:
            self.last_seq += 1
            event = self.pending_events.pop(self.last_seq)
            if event["method"] == "process/exited":
                self.exited, self.exit_code = True, event["exitCode"]
            elif event["method"] == "process/closed":
                self.closed = True
            self.events.publish(event)

    def publish(self, method: str, params: dict) -> None:
        seq = params["seq"]
        if seq <= self.last_seq or self.failure is not None or self.closed:
            return
        event = {"method": method, **params}
        if method == "process/output":
            event["chunk"] = decode_bytes(params["chunk"], "chunk")
        if seq in self.pending_events:
            return
        closes_gap = seq == self.last_seq + 1
        size = event_bytes(event)
        if size > 1024 * 1024:
            raise ExecutorProtocolError("process event exceeds 1048576 bytes")
        if not closes_gap and (len(self.pending_events) >= 256
                               or sum(event_bytes(item) for item in self.pending_events.values()) + size > 1024 * 1024):
            raise ExecutorProtocolError("process event reorder buffer exceeds Source bounds")
        self.pending_events[seq] = event
        self._publish_ready()

    def fail(self, error: Exception) -> None:
        if self.failure is None and not self.closed:
            self.failure = error
            self.pending_events.clear()
            self.events.publish({"method": "process/failed", "error": str(error)})

    def _recover_response(self, response: ProcessRead) -> None:
        if response.failure:
            raise ExecutorProtocolError(f"process failed while recovering: {response.failure}")
        target = max(0, response.next_seq - 1)
        if self.failure is not None or self.closed or target <= self.last_seq:
            return
        exit_known = self.exited or any(event["method"] == "process/exited"
                                       for seq, event in self.pending_events.items() if seq <= target)
        if response.closed and (any(chunk.seq == target for chunk in response.chunks)
                                or (target in self.pending_events and self.pending_events[target]["method"] != "process/closed")):
            raise ExecutorProtocolError("process close sequence conflicts with recovered output")
        for chunk in response.chunks:
            if chunk.seq > target:
                raise ExecutorProtocolError("recovered process output exceeds target sequence")
            if response.exited and not exit_known and chunk.seq > self.last_seq + 1:
                if response.exit_code is None:
                    raise ExecutorProtocolError("recovering exited process did not include its exit code")
                self.publish("process/exited", {"processId": self.process_id, "seq": self.last_seq + 1,
                                               "exitCode": response.exit_code, "sandboxDenied": response.sandbox_denied})
                exit_known = True
            self.publish("process/output", {"processId": self.process_id, "seq": chunk.seq,
                                           "stream": chunk.stream, "chunk": base64.b64encode(chunk.chunk).decode("ascii")})
        if response.closed:
            self.publish("process/closed", {"processId": self.process_id, "seq": target})
        retained = sorted(seq for seq in self.pending_events if self.last_seq < seq <= target)
        missing_count = target - self.last_seq - len(retained)
        if response.exited and not exit_known:
            if missing_count != 1 or response.exit_code is None:
                raise ExecutorProtocolError("process event recovery has a sequence gap")
            first_missing = self.last_seq + 1
            for seq in retained:
                if seq != first_missing:
                    break
                first_missing += 1
            self.publish("process/exited", {"processId": self.process_id, "seq": first_missing,
                                           "exitCode": response.exit_code, "sandboxDenied": response.sandbox_denied})
        elif missing_count:
            raise ExecutorProtocolError("process event recovery has a sequence gap")
        self._publish_ready()

    async def recover(self, rpc) -> None:
        try:
            data = await rpc.request("process/read", {"processId": self.process_id, "afterSeq": self.last_seq,
                                                       "maxBytes": None, "waitMs": 0})
            self._recover_response(ProcessRead.from_wire(data))
        except ExecutorDisconnected:
            raise
        except Exception as error:
            await rpc.request("process/terminate", {"processId": self.process_id}, cleanup=True)
            self.fail(error)

    async def read(self, *, after_seq: int | None = None, max_bytes: int | None = None,
                   wait_ms: int | None = None) -> ProcessRead:
        if self.failure is not None:
            return ProcessRead((), self.last_seq + 1, self.exited, self.exit_code, True, str(self.failure))
        data = await self.client.request("process/read", {"processId": self.process_id, "afterSeq": after_seq,
                                                          "maxBytes": max_bytes, "waitMs": wait_ms})
        return ProcessRead.from_wire(data)

    async def next_event(self) -> dict:
        if self.default_receiver is None:
            self.default_receiver = self.subscribe_events()
        return await self.default_receiver.recv()

    def subscribe_events(self):
        return self.events.subscribe()

    async def write(self, chunk: bytes, *, write_id: str | None = None) -> str:
        data = await self.client.request("process/write", {"processId": self.process_id,
            "chunk": base64.b64encode(chunk).decode("ascii"), "writeId": write_id or uuid4().hex})
        return data["status"]

    async def signal(self, signal: str = "interrupt") -> None:
        await self.client.request("process/signal", {"processId": self.process_id, "signal": signal})

    async def terminate(self) -> bool:
        result = await self.client.request("process/terminate", {"processId": self.process_id}, cleanup=True)
        return result["running"]

    async def _close(self) -> dict:
        try:
            unconfirmed = False
            if self.start_task is not None:
                try:
                    await asyncio.shield(self.start_task)
                except ExecutorRpcError:
                    pass  # The caller retains the peer's explicit start rejection.
                except Exception:
                    unconfirmed = True
            if (self.accepted or unconfirmed) and not self.closed:
                await self.terminate()
                while not self.closed:
                    data = await self.client.request("process/read", {"processId": self.process_id,
                        "afterSeq": self.last_seq, "maxBytes": None, "waitMs": 1000}, cleanup=True)
                    read = ProcessRead.from_wire(data)
                    if self.failure is None:
                        self._recover_response(read)
                    elif read.closed:
                        # A broken output stream remains broken. The original
                        # peer's close receipt can still prove tree cleanup.
                        self.closed = True
                        self.exited, self.exit_code = read.exited, read.exit_code
            return {"process_id": self.process_id, "closed": self.closed, "exit_code": self.exit_code,
                    "failure": str(self.failure) if self.failure else None}
        except (ExecutorDisconnected, ExecutorClosed) as error:
            self.close_failure = error
            return {"process_id": self.process_id, "closed": self.closed, "exit_code": self.exit_code,
                "failure": str(self.failure) if self.failure else None, "cleanup_failure": str(error)}
        finally:
            if self.closed or not self.accepted and not unconfirmed:
                self.released = True
                if self.client.processes.get(self.process_id) is self:
                    self.client.processes.pop(self.process_id)
                self.client.notify_idle()

    async def aclose(self) -> dict:
        if self.close_task is not None and self.close_task.done() and not self.released:
            self.close_task = None  # Retry this original handle after an unconfirmed close.
        if self.close_task is None:
            self.close_task = asyncio.create_task(self._close(), name="executor-process-close")
        return await asyncio.shield(self.close_task)

    async def __aenter__(self) -> ExecutorProcess:
        return self

    async def __aexit__(self, *_args) -> None:
        await self.aclose()


class ProcessBackend:
    def __init__(self, environment):
        self.environment = environment

    async def start(self, argv: list[str], cwd: str, *, sandbox: dict | None,
                    env: dict[str, str] | None = None, tty: bool = False, pipe_stdin: bool = False,
                    arg0: str | None = None, process_id: str | None = None,
                    env_policy: dict | None = None, metadata: dict | None = None,
                    shell_snapshot: dict | None = None, enforce_managed_network: bool = False,
                    managed_network: dict | None = None, network_proxy: dict | None = None,
                    process_observer=None, network_decider=None, network_observer=None) -> ExecutorProcess:
        cwd = file_uri(cwd)
        sandbox = deepcopy(sandbox)
        client = await self.environment.get_client()
        if network_proxy is not None:
            info = client.environment_info if client.environment_info is not None else await client.info()
            if info["capabilities"].get("networkProxyLaunch") is not True:
                raise ExecutorProtocolError("selected exec-server does not support executor-local network proxy launches")
        process_id = process_id or uuid4().hex
        if process_id in client.processes:
            raise ExecutorProtocolError("process handle is already registered in this executor session")
        process = ExecutorProcess(client, process_id, sandbox)
        process.network_decider = network_decider
        process.network_observer = network_observer
        client.processes[process_id] = process
        if process_observer is not None:
            try:
                process_observer(process)
            except BaseException:
                client.processes.pop(process_id)
                client.notify_idle()
                raise
        params: dict[str, Any] = {"processId": process_id, "argv": list(argv), "cwd": cwd,
            "env": dict(env or {}), "tty": tty, "pipeStdin": pipe_stdin, "arg0": arg0,
            "sandbox": process.sandbox, "envPolicy": deepcopy(env_policy),
            "enforceManagedNetwork": enforce_managed_network, "managedNetwork": deepcopy(managed_network)}
        for key, value in (("metadata", metadata), ("shellSnapshot", shell_snapshot), ("networkProxy", network_proxy)):
            if value is not None:
                params[key] = deepcopy(value)

        async def start() -> None:
            response = await client.request("process/start", params)
            if response["processId"] != process_id:
                raise ExecutorProtocolError("exec-server returned another process handle")
            process.sandbox_type = response.get("sandboxType")
            process.accepted = True

        process.start_task = asyncio.create_task(start(), name="executor-process-start")
        try:
            await asyncio.shield(process.start_task)
            return process
        except asyncio.CancelledError:
            client.retain_cleanup(process.aclose())
            raise
        except ExecutorRpcError as error:
            process.fail(error)
            process.released = True
            client.processes.pop(process_id, None)
            client.notify_idle()
            raise
        except Exception as error:
            # The peer may have accepted start before its reply was lost.
            # Resume the original session and terminate that logical ID;
            # never send process/start a second time.
            process.fail(error)
            client.retain_cleanup(process.aclose())
            raise
