"""Lazy process host shared by code sessions on the application's event loop."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
import sys
from uuid import uuid4
from weakref import WeakKeyDictionary

from backend.agent.code_host_protocol import PROTOCOL_VERSION, encode_frame, read_frame
from backend.atomic_io import await_task_despite_cancellation
from backend.runtime_env import sanitized_subprocess_env
from backend.subprocesses import spawn_exec, terminate_process_tree

logger = logging.getLogger(__name__)


class CodeHostError(RuntimeError):
    pass


class CodeHostDisconnected(CodeHostError):
    pass


class _Connection:
    def __init__(self, process):
        self.process = process
        self.closed = asyncio.Event()
        self.failure: CodeHostDisconnected | None = None
        self._closing = False
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._write_lock = asyncio.Lock()
        self.stderr = bytearray()
        self._stderr_task = asyncio.create_task(self._read_stderr(), name="code-host-stderr")
        self._reader = asyncio.create_task(self._read(), name="code-host-responses")
        logger.info("Code host started (pid %s)", process.pid)

    @classmethod
    async def start(cls):
        process = await spawn_exec(sys.executable, "-I", str(Path(__file__).with_name("code_host_worker.py")),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=sanitized_subprocess_env())
        try:
            async with asyncio.timeout(30):
                process.stdin.write(encode_frame({"hello": PROTOCOL_VERSION}))
                await process.stdin.drain()
                hello = await read_frame(process.stdout)
                if hello != {"ready": PROTOCOL_VERSION, "capabilities": ["cancel", "session_isolation"]}:
                    raise CodeHostError("Code host rejected the protocol handshake")
        except BaseException:
            if not await terminate_process_tree(process):
                raise CodeHostError(f"Code host startup cleanup remains pending (pid {process.pid})")
            raise
        return cls(process)

    @property
    def alive(self) -> bool:
        return not self.closed.is_set() and self.process.returncode is None and not self._closing

    async def _read_stderr(self) -> None:
        while chunk := await self.process.stderr.read(4096):
            self.stderr.extend(chunk)
            del self.stderr[:-8192]

    async def _read(self) -> None:
        reason = "Code host closed"
        try:
            while True:
                message = await read_frame(self.process.stdout)
                future = self._pending.pop(message["id"])
                if "error" in message:
                    future.set_exception(CodeHostError(message["error"]))
                else:
                    future.set_result(message["result"])
        except asyncio.CancelledError:
            reason = "Code host controller stopped"
            raise
        except Exception as error:
            reason = f"Code host connection ended ({type(error).__name__}: {error})"
        finally:
            self.failure = CodeHostDisconnected(reason + "; live cells cannot be resumed. Inspect tool outcomes before retrying writes.")
            self.closed.set()
            for future in self._pending.values():
                future.set_exception(self.failure)
            self._pending.clear()
            self.process.stdin.close()
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except TimeoutError:
                if not await terminate_process_tree(self.process):
                    raise CodeHostError(f"Code host cleanup remains pending (pid {self.process.pid})")
            await self._stderr_task
            if not self._closing and self.stderr:
                logger.warning("Code host stderr: %s", self.stderr.decode("utf-8", errors="replace"))
            logger.info("Code host stopped (pid %s, exit %s)", self.process.pid, self.process.returncode)

    async def _write(self, message: dict) -> None:
        frame = encode_frame(message)
        async with self._write_lock:
            self.process.stdin.write(frame)
            await self.process.stdin.drain()

    async def request(self, method: str, **payload):
        if not self.alive:
            raise self.failure or CodeHostDisconnected("Code host has closed")
        self._next_id += 1
        request_id = self._next_id
        frame = encode_frame({"id": request_id, "method": method, **payload})
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        sent = False
        try:
            async with self._write_lock:
                self.process.stdin.write(frame)
                sent = True
                await self.process.stdin.drain()
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            # Keep the reply owned after the caller stops observing it. The
            # subsequent cell close waits for V8 cancellation to complete.
            if not sent:
                self._pending.pop(request_id)
                raise
            future.add_done_callback(lambda done: done.exception())
            if self.alive and method == "step":
                await self._write({"cancel": request_id})
            raise
        except OSError:
            future.add_done_callback(lambda done: done.exception())
            self.process.stdin.close()
            raise

    async def aclose(self) -> None:
        self._closing = True
        self.process.stdin.close()
        await asyncio.shield(self._reader)


class CodeHost:
    def __init__(self):
        self.sessions: set[CodeHostSession] = set()
        self._connection: _Connection | None = None
        self._connecting: asyncio.Task | None = None

    async def _connect(self) -> _Connection:
        if self._connection is not None:
            await self._connection.aclose()
        self._connection = await _Connection.start()
        return self._connection

    async def connection(self) -> _Connection:
        if self._connection is not None and self._connection.alive:
            return self._connection
        if self._connecting is None or self._connecting.done():
            self._connecting = asyncio.create_task(self._connect(), name="code-host-start")
        return await asyncio.shield(self._connecting)

    async def release(self, session: CodeHostSession) -> None:
        self.sessions.remove(session)
        if not self.sessions:
            if self._connecting is not None:
                await asyncio.gather(self._connecting, return_exceptions=True)
            if not self.sessions and self._connection is not None:
                closing = self._connection
                await closing.aclose()
                if not self.sessions and self._connection is closing:
                    self._connection = None
                    self._connecting = None


_hosts: WeakKeyDictionary = WeakKeyDictionary()


class CodeHostSession:
    def __init__(self):
        self.host = _hosts.setdefault(asyncio.get_running_loop(), CodeHost())
        self.host.sessions.add(self)
        self._binding: tuple[_Connection, str] | None = None
        self._lock = asyncio.Lock()
        self._close_task: asyncio.Task | None = None

    async def binding(self) -> tuple[_Connection, str]:
        async with self._lock:
            if self._close_task is not None:
                raise CodeHostError("Code host session has closed")
            if self._binding is None or not self._binding[0].alive:
                connection = await self.host.connection()
                session_id = "session_" + uuid4().hex
                # Retain the binding before dispatch so cancellation also
                # closes a remotely accepted, locally unobserved open.
                self._binding = (connection, session_id)
                opening = asyncio.create_task(connection.request("open", session=session_id), name="code-host-session-open")
                try:
                    await asyncio.shield(opening)
                except asyncio.CancelledError:
                    await await_task_despite_cancellation(opening)
                    raise
            return self._binding

    def cell(self) -> HostedCodeVM:
        return HostedCodeVM(self)

    async def _close(self) -> None:
        async with self._lock:
            try:
                if self._binding is not None:
                    connection, session_id = self._binding
                    if connection.alive:
                        await connection.request("shutdown", session=session_id)
                    else:
                        await connection.aclose()
            finally:
                await self.host.release(self)

    async def aclose(self) -> None:
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close(), name="code-host-session-close")
        try:
            await asyncio.shield(self._close_task)
        except asyncio.CancelledError:
            await await_task_despite_cancellation(self._close_task)
            raise


class HostedCodeVM:
    def __init__(self, session: CodeHostSession):
        self.session = session
        self.id = "isolate_" + uuid4().hex
        self._binding: tuple[_Connection, str] | None = None
        self._closed = False
        self._close_task: asyncio.Task | None = None
        self._created = False

    async def _prepare(self) -> None:
        self._binding = await self.session.binding()
        connection, session_id = self._binding
        await connection.request("create", session=session_id, cell=self.id)
        self._created = True

    async def step(self, operation: str = "", payload=None) -> dict:
        if self._closed:
            raise CodeHostError("Code cell has closed")
        if self._binding is None:
            opening = asyncio.create_task(self._prepare(), name="code-host-cell-open")
            try:
                await asyncio.shield(opening)
            except asyncio.CancelledError:
                await await_task_despite_cancellation(opening)
                raise
        connection, session_id = self._binding
        return await connection.request("step", session=session_id, cell=self.id, operation=operation, payload=payload)

    async def wait_failure(self) -> None:
        connection, _ = self._binding
        await connection.closed.wait()
        raise connection.failure

    async def aclose(self) -> None:
        if self._close_task is None:
            self._closed = True
            self._close_task = asyncio.create_task(self._close(), name="code-host-cell-close")
        try:
            await asyncio.shield(self._close_task)
        except asyncio.CancelledError:
            await await_task_despite_cancellation(self._close_task)
            raise

    async def _close(self) -> None:
        if self._binding is not None:
            connection, session_id = self._binding
            if connection.alive and self._created:
                await connection.request("close", session=session_id, cell=self.id)
            elif not connection.alive:
                await connection.aclose()
