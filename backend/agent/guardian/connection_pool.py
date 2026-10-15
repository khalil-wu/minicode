"""Source Luna Snapshot connection leases, background prewarm and HTTP selection."""
from __future__ import annotations

import asyncio
import time
from contextlib import aclosing
from dataclasses import dataclass

import httpx
from websockets.exceptions import InvalidHandshake
from websockets.protocol import State

from backend.llm.base import emit_provider_lifecycle_response
from backend.ids import new_uuid7 as new_classification_id
from backend.llm.provider_contracts import ProviderRegistrationError
from backend.llm.responses_websocket import WEBSOCKET_BETA, connect


INITIAL_WEBSOCKET_CONNECTIONS = 8
MAX_CONCURRENT_REQUESTS = 16
CONNECT_COOLDOWN = 5 * 60
MAX_WEBSOCKET_AGE = 55 * 60
WEBSOCKET_CONNECT_TIMEOUT = 15.0
STREAM_IDLE_TIMEOUT = 300.0


@dataclass
class GuardianClientSetup:
    adapter: object
    url: str
    proxy: str | None
    headers: dict
    key: tuple
    classifier_route: bool


@dataclass
class PooledConnection:
    socket: object
    thread_id: str
    key: tuple
    classifier_route: bool
    expires_at: float


class GuardianConnectionLease:
    def __init__(self, pool, setup, thread_id, connection):
        self.pool = pool
        self.setup = setup
        self.thread_id = thread_id
        self.connection = connection
        self.classifier_route = connection.classifier_route if connection is not None else setup.classifier_route
        self.completed = False
        self.closed = False
        self.stream_idle_timeout = pool.stream_idle_timeout
        pool.active.add(self)

    @property
    def mode(self):
        return "websocket" if self.connection is not None else "http"

    async def events(self, request, *, metadata, cancel_event):
        if self.connection is None:
            events = self.setup.adapter.guardian_classification_events(request,
                headers=self.setup.headers, metadata=metadata, cancel_event=cancel_event,
                stream_idle_timeout=self.stream_idle_timeout)
        else:
            events = self.setup.adapter.guardian_classification_websocket_events(request,
                socket=self.connection.socket, headers=self.setup.headers,
                metadata=metadata, cancel_event=cancel_event,
                stream_idle_timeout=self.stream_idle_timeout)
        async with aclosing(events):
            async for event in events:
                yield event

    async def aclose(self):
        if self.closed:
            return
        self.closed = True
        self.pool.active.remove(self)
        connection, self.connection = self.connection, None
        try:
            if connection is not None:
                if self.completed and not self.pool.closed:
                    self.pool.idle.append(connection)
                else:
                    await self.pool.close_connection(connection)
        finally:
            self.pool.classifications.release()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.aclose()


class GuardianConnectionPool:
    def __init__(self, setup_source, *, clock=time.monotonic):
        self.setup_source = setup_source
        self.clock = clock
        self.idle = []
        self.active = set()
        self.classifications = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
        self.sockets = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
        self.replenishing = None
        self.retry_after = None
        self.connect_timeout = WEBSOCKET_CONNECT_TIMEOUT
        self.stream_idle_timeout = STREAM_IDLE_TIMEOUT
        self.closed = False
        self.audit = []

    def replenish(self, context):
        if self.closed or self.replenishing is not None:
            return None
        if self.retry_after is not None and self.clock() < self.retry_after:
            return None
        task = asyncio.create_task(self._replenish(context))
        self.replenishing = task
        task.add_done_callback(self._replenished)
        return task

    def _replenished(self, task):
        self.replenishing = None
        if not task.cancelled() and task.exception() is not None:
            self.audit.append({"operation": "prewarm", "status": "failed", "error": str(task.exception())})

    async def _replenish(self, context):
        while len(self.idle) < INITIAL_WEBSOCKET_CONNECTIONS and not self.sockets.locked():
            await self.sockets.acquire()
            socket = None
            retained = False
            started = self.clock()
            try:
                thread_id = new_classification_id()
                setup = await self.setup_source(context, thread_id)
                socket = await connect(setup.url, additional_headers={**setup.headers, "OpenAI-Beta": WEBSOCKET_BETA},
                    proxy=setup.proxy, max_size=None, max_queue=1,
                    open_timeout=self.connect_timeout, close_timeout=1)
                await emit_provider_lifecycle_response(_context_metadata(context), socket.response.status_code, socket.response.headers)
                self.idle.append(PooledConnection(socket, thread_id, setup.key,
                    setup.classifier_route, self.clock() + MAX_WEBSOCKET_AGE))
                socket = None
                retained = True
                self.retry_after = None
                self.audit.append({"operation": "prewarm", "status": "completed", "duration_ms": int((self.clock() - started) * 1000)})
            except (TimeoutError, httpx.TransportError, InvalidHandshake, OSError, ProviderRegistrationError) as error:
                if isinstance(error, TimeoutError):
                    self.retry_after = self.clock() + CONNECT_COOLDOWN
                self.audit.append({"operation": "prewarm", "status": "failed", "error": str(error),
                    "duration_ms": int((self.clock() - started) * 1000)})
                return
            finally:
                if socket is not None:
                    await socket.close()
                # Only an idle connection takes ownership of the socket permit.
                if not retained:
                    self.sockets.release()

    async def lease(self, context):
        await self.classifications.acquire()
        try:
            thread_id = new_classification_id()
            setup = await self.setup_source(context, thread_id)
            while self.idle:
                connection = self.idle.pop()
                if (connection.key == setup.key and self.clock() < connection.expires_at
                        and connection.socket.state == State.OPEN):
                    setup.headers.update({"thread_id": connection.thread_id,
                        "x-codex-window-id": f"{connection.thread_id}:0", "x-client-request-id": connection.thread_id})
                    return GuardianConnectionLease(self, setup, connection.thread_id, connection)
                await self.close_connection(connection)
            self.replenish(context)
            return GuardianConnectionLease(self, setup, thread_id, None)
        except BaseException:
            self.classifications.release()
            raise

    async def close_connection(self, connection):
        try:
            await connection.socket.close()
        finally:
            self.sockets.release()

    async def prewarm(self, context):
        task = self.replenish(context)
        if task is not None:
            await task

    async def aclose(self):
        self.closed = True
        task = self.replenishing
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        idle, self.idle = self.idle, []
        await asyncio.gather(*(lease.aclose() for lease in tuple(self.active)),
            *(self.close_connection(connection) for connection in idle))


def _context_metadata(context):
    turn = context.run_context.llm_turn_context if context.run_context is not None else None
    return turn.request_metadata() if turn is not None else {}
