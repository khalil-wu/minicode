"""Captured MCP connections shared by step views of one receiving thread."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
import time
import threading
from collections import OrderedDict
from typing import Any, Awaitable, Callable

from backend.async_cleanup import _consume_task_result


@dataclass(frozen=True)
class MCPStartupResult:
    client: Any
    tools: tuple
    instructions: str


class MCPConnection:
    """One initialize/tools-list attempt; a view's timeout never owns that attempt."""

    def __init__(self, *, factory: Callable[[MCPConnection], Awaitable[Any]],
            close: Callable[[Any], Awaitable[None]], identity: Any) -> None:
        self.identity = identity
        self.factory = factory
        self.close_client = close
        self.client = None
        self.task: asyncio.Task[MCPStartupResult] | None = None
        self.views: set[MCPConnectionView] = set()
        self.retired = False
        self.closed = False
        self._startup_deadline: tuple[int, float] | None = None
        self._startup_slot: asyncio.Semaphore | None = None
        self._close_task: asyncio.Task[None] | None = None
        self.catalog_identity = None
        self.catalog_cache = None
        self.current_result: MCPStartupResult | None = None
        self.catalog_error: BaseException | None = None
        self.startup_error: BaseException | None = None
        self._catalog_refresh: asyncio.Task[MCPStartupResult] | None = None
        self._catalog_dirty = False
        self._catalog_change_revision = 0
        self._client_closed = False

    def acquire(self, *, on_disconnect, on_tools_changed, on_release=None) -> MCPConnectionView:
        view = MCPConnectionView(self, on_disconnect, on_tools_changed, on_release)
        self.views.add(view)
        return view

    def start(self, *, startup_slot: asyncio.Semaphore | None = None) -> asyncio.Task[MCPStartupResult]:
        if self.closed:
            raise RuntimeError("The captured MCP connection is closed")
        if self.task is None:
            self._startup_slot = startup_slot
            self.task = asyncio.create_task(self._start(), name="mcp-connection-startup")
            self.task.add_done_callback(_consume_task_result)
        return self.task

    async def _start(self) -> MCPStartupResult:
        try:
            if self._startup_slot is not None:
                async with self._startup_slot:
                    return await self._initialize()
            return await self._initialize()
        except BaseException as exc:
            self.startup_error = exc
            await self._close_client()
            raise

    async def _initialize(self) -> MCPStartupResult:
        ticket = self.catalog_cache.begin_fetch() if self.catalog_cache is not None else None
        factory, self.factory = self.factory, None
        self.client = await factory(self)
        await self.client.connect()
        if self.catalog_cache is not None and not self.client.tool_catalog_cacheable:
            self.catalog_cache.disable()
        tools = await self.client.list_tools()
        result = MCPStartupResult(self.client, tuple(tools), self.client.instructions)
        self.current_result = result
        if self.catalog_cache is not None:
            self.catalog_cache.publish(ticket, result.tools, result.instructions)
        return result

    def attach_catalog(self, cache, identity, *, context=None) -> None:
        if self.catalog_cache is not None:
            return
        self.catalog_cache = context if context is not None else cache.context(identity)
        self.catalog_identity = identity

    async def ready(self) -> MCPStartupResult:
        result = await asyncio.shield(self.start())
        while self._catalog_dirty:
            result = await self.refresh_catalog()
        if self._catalog_refresh is not None and not self._catalog_refresh.done():
            result = await asyncio.shield(self._catalog_refresh)
        if self.catalog_error is not None:
            raise self.catalog_error
        if not result.client.connected:
            raise ConnectionError("The captured MCP connection is no longer connected")
        return self.current_result or result

    async def refresh_catalog(self) -> MCPStartupResult:
        if not self._catalog_dirty and self.catalog_error is None:
            return self.current_result
        if self._catalog_refresh is None or self._catalog_refresh.done():
            self._catalog_refresh = asyncio.create_task(self._refresh_catalog(), name="mcp-connection-tool-list")
            self._catalog_refresh.add_done_callback(_consume_task_result)
        return await asyncio.shield(self._catalog_refresh)

    async def _refresh_catalog(self) -> MCPStartupResult:
        ticket = self.catalog_cache.begin_fetch() if self.catalog_cache is not None else None
        revision = self._catalog_change_revision
        try:
            tools = await self.client.list_tools()
            result = MCPStartupResult(self.client, tuple(tools), self.client.instructions)
            self.current_result, self.catalog_error = result, None
            self._catalog_dirty = revision != self._catalog_change_revision
            if self.catalog_cache is not None:
                self.catalog_cache.publish(ticket, tools, result.instructions)
            return result
        except BaseException as exc:
            self.catalog_error = exc
            raise

    def startup_deadline(self, grace_ms: int, default_deadline: float) -> float:
        if self._startup_deadline is None or self._startup_deadline[0] != grace_ms:
            self._startup_deadline = (grace_ms, default_deadline)
        return self._startup_deadline[1]

    async def notify_disconnect(self, server_name: str) -> None:
        for view in tuple(self.views):
            if not view.released:
                await view.on_disconnect(server_name)

    async def notify_tools_changed(self, server_name: str) -> None:
        self._catalog_dirty = True
        self._catalog_change_revision += 1
        if self.catalog_cache is not None:
            self.catalog_cache.invalidate()
        for view in tuple(self.views):
            if not view.released:
                await view.on_tools_changed(server_name)

    async def _close(self) -> None:
        self.retired = True
        tasks = [task for task in (self.task, self._catalog_refresh) if task is not None and not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._close_client()
        self.factory = None
        self.closed = True

    async def _close_client(self) -> None:
        if not self._client_closed:
            await self.close_client(self.client)
            self._client_closed = True

    async def aclose(self) -> None:
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close(), name="mcp-connection-close")
        await asyncio.shield(self._close_task)

    async def cancel_startup(self) -> None:
        if self.task is not None and not self.task.done():
            await self.aclose()

    def startup_pending(self) -> bool:
        return self.current_result is None and self.startup_error is None and (self.task is None or not self.task.done())


@dataclass(eq=False)
class MCPConnectionView:
    connection: MCPConnection
    on_disconnect: Callable[[str], Awaitable[None]]
    on_tools_changed: Callable[[str], Awaitable[None]]
    on_release: Callable[[MCPConnection], Awaitable[None]] | None = None
    released: bool = False

    @property
    def client(self):
        return self.connection.client

    async def wait_for_startup(self) -> MCPStartupResult:
        return await self.connection.ready()

    async def aclose(self) -> bool:
        if not self.released:
            self.released = True
            self.connection.views.remove(self)
            if self.on_release is not None:
                await self.on_release(self.connection)
        return True


@dataclass(frozen=True)
class MCPCachedCatalog:
    tools: tuple
    instructions: str
    published_at: float
    generation: int
    revision: int

    def current(self) -> bool:
        return time.monotonic() - self.published_at <= 30 * 60

    def copy_tools(self):
        return list(deepcopy(self.tools))


@dataclass
class _CatalogEntry:
    snapshot: MCPCachedCatalog | None = None
    deadline: tuple[int, float] | None = None
    revision: int = 0
    disabled: bool = False


class MCPCatalogContext:
    def __init__(self, cache, entry):
        self.cache, self.entry = cache, entry

    def get(self, fallback=None) -> MCPCachedCatalog | None:
        with self.cache.lock:
            entry = self.entry
            snapshot = entry.snapshot
            if entry.disabled or snapshot is not None and snapshot.revision != entry.revision:
                return None
            return snapshot if snapshot is not None and snapshot.current() else fallback

    def deadline(self, grace_ms: int, default_deadline: float) -> float:
        with self.cache.lock:
            entry = self.entry
            if entry.disabled or entry.snapshot is not None and entry.snapshot.current() and entry.snapshot.revision == entry.revision:
                return default_deadline
            if entry.deadline is None or entry.deadline[0] != grace_ms:
                entry.deadline = (grace_ms, default_deadline)
            return entry.deadline[1]

    def begin_fetch(self):
        with self.cache.lock:
            self.cache.generation += 1
            return self.cache.generation, self.entry.revision

    def publish(self, ticket, tools, instructions: str) -> None:
        with self.cache.lock:
            entry = self.entry
            generation, revision = ticket
            if entry.disabled or entry.snapshot is not None and generation <= entry.snapshot.generation:
                return
            copied = deepcopy(tuple(tools))
            for tool in copied:
                tool.annotations = {}
            entry.snapshot = MCPCachedCatalog(copied, instructions, time.monotonic(), generation, revision)
            entry.deadline = None

    def invalidate(self) -> None:
        with self.cache.lock:
            self.entry.revision += 1

    def disable(self) -> None:
        with self.cache.lock:
            self.entry.disabled, self.entry.snapshot = True, None


class MCPToolCatalogCache:
    def __init__(self) -> None:
        self.entries: OrderedDict[Any, _CatalogEntry] = OrderedDict()
        self.generation = 0
        self.lock = threading.RLock()

    def _entry(self, identity):
        entry = self.entries.pop(identity, None) or _CatalogEntry()
        self.entries[identity] = entry
        while len(self.entries) > 32:
            self.entries.popitem(last=False)
        return entry

    def context(self, identity) -> MCPCatalogContext | None:
        if identity is None:
            return None
        with self.lock:
            return MCPCatalogContext(self, self._entry(identity))


TOOL_CATALOG_CACHE = MCPToolCatalogCache()


class MCPConnectionPool:
    """The thread owns producers; per-step MCP managers own borrowed views."""

    def __init__(self) -> None:
        self.connections: dict[Any, MCPConnection] = {}
        self.active: dict[str, MCPConnection] = {}
        self.catalog_cache = TOOL_CATALOG_CACHE
        self._retired: set[MCPConnection] = set()
        self._retirement_tasks: set[asyncio.Task] = set()
        self.closed = False

    def acquire(self, *, name: str, identity: Any, catalog_identity: Any,
            capture, on_disconnect, on_tools_changed) -> MCPConnectionView:
        if self.closed:
            raise RuntimeError("The receiving thread's MCP connection owner is closed")
        previous = self.active.get(name)
        connection = self.connections.get(identity)
        if connection is None or connection.closed or connection.retired:
            factory, close = capture()
            connection = MCPConnection(factory=factory, close=close, identity=identity)
            connection.attach_catalog(self.catalog_cache, catalog_identity)
            self.connections[identity] = connection
        self.active[name] = connection
        view = connection.acquire(on_disconnect=on_disconnect, on_tools_changed=on_tools_changed,
            on_release=self._release)
        if previous is not None and previous is not connection:
            self._retire(previous)
        return view

    def _retire(self, connection) -> None:
        connection.retired = True
        self._retired.add(connection)
        if self.connections.get(connection.identity) is connection:
            self.connections.pop(connection.identity)
        if not connection.views:
            task = asyncio.create_task(self._release(connection), name="mcp-connection-retire")
            self._retirement_tasks.add(task)
            task.add_done_callback(self._retirement_tasks.discard)
            task.add_done_callback(_consume_task_result)

    async def _release(self, connection) -> None:
        if connection.retired and not connection.views:
            await connection.aclose()
            self._retired.discard(connection)

    def catalog(self, identity: Any) -> MCPCachedCatalog | None:
        context = self.catalog_cache.context(identity)
        return context.get() if context is not None else None

    async def restart(self, name: str) -> None:
        connection = self.active.pop(name, None)
        if connection is not None:
            self._retire(connection)
            await connection.aclose()

    async def reconnect(self, name: str, failed: MCPConnection) -> None:
        """All observers of one dead transport join one replacement generation."""
        if self.active.get(name) is failed:
            self.active.pop(name)
            self._retire(failed)
        await failed.aclose()

    def reconcile(self, names) -> None:
        retiring = [connection for name, connection in self.active.items() if name not in names]
        self.active = {name: connection for name, connection in self.active.items() if name in names}
        for connection in retiring:
            self._retire(connection)

    async def cancel_startup(self) -> None:
        connections = set(self.connections.values()) | self._retired
        await asyncio.gather(*(connection.cancel_startup() for connection in connections))

    async def aclose(self) -> None:
        self.closed = True
        connections = set(self.connections.values()) | self._retired
        await asyncio.gather(*(connection.aclose() for connection in connections))
        if self._retirement_tasks:
            await asyncio.gather(*tuple(self._retirement_tasks))
        self.connections.clear()
        self.active.clear()
        self._retired.clear()
