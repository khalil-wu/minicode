"""Managed application destinations and revocable HTTP/WebSocket operations.

This is independent of permissions.network and tool-process sandbox leases.
The configuration owner publishes policy; transports only acquire permits.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import Future
from dataclasses import dataclass
import threading
from urllib.parse import urlsplit
from weakref import WeakSet

import httpx
from websockets.asyncio.client import connect as WebSocketConnect


class ApplicationNetworkDenied(PermissionError):
    def __init__(self, reason: str):
        self.reason = reason
        messages = {
            "unavailable": "application network policy is unavailable",
            "destination": "destination denied by application network policy",
            "revoked": "application network permission was revoked",
            "unsupported_transport": "this SDK transport is disabled by application network restrictions",
        }
        super().__init__("provider_error_type=application_network: " + messages[reason])


@dataclass(frozen=True)
class DestinationPolicy:
    allowed_hosts: frozenset[str] | None = None

    def allows(self, destination: str | None) -> bool:
        if self.allowed_hosts is None:
            return True
        if destination is None:
            return False
        url = urlsplit(destination)
        return url.scheme in {"https", "wss"} and (url.hostname or "").removesuffix(".").lower() in self.allowed_hosts


class NetworkPermit:
    def __init__(self, destination: str | None):
        self.destination = destination
        self.revocation = Future()
        self.lock = threading.Lock()
        self.events = {}
        self.revocation.add_done_callback(self._notify)

    def _notify(self, _future):
        with self.lock:
            for loop, event in self.events.items():
                if not loop.is_closed():
                    loop.call_soon_threadsafe(event.set)

    def check(self):
        if self.revocation.done():
            raise ApplicationNetworkDenied("revoked")

    def _event(self):
        loop = asyncio.get_running_loop()
        with self.lock:
            if loop not in self.events:
                self.events[loop] = asyncio.Event()
            event = self.events[loop]
            if self.revocation.done():
                event.set()
        return event

    async def revoked(self):
        await self._event().wait()

    async def run(self, operation):
        self.check()
        event = self._event()
        task = asyncio.ensure_future(operation())
        revoked = asyncio.create_task(event.wait())
        try:
            await asyncio.wait((task, revoked), return_when=asyncio.FIRST_COMPLETED)
            self.check()
            return task.result()
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            revoked.cancel()
            await asyncio.gather(revoked, return_exceptions=True)


class ApplicationNetworkPolicy:
    def __init__(self, controller=None, generation=None):
        self.controller = controller
        self.generation = generation

    @property
    def managed(self):
        return self.controller is not None

    def for_current_generation(self):
        return ApplicationNetworkPolicy(self.controller, self.controller.revision if self.managed else None)

    def acquire(self, destination: str):
        return self._acquire(destination)

    def acquire_for_unsupported_sdk(self):
        return self._acquire(None)

    def _acquire(self, destination):
        permit = NetworkPermit(destination)
        if not self.managed:
            return permit
        with self.controller.lock:
            if self.generation is not None and self.generation != self.controller.revision:
                raise ApplicationNetworkDenied("revoked")
            policy = self.controller.destination_policy
            if policy is None:
                raise ApplicationNetworkDenied("unavailable")
            if not policy.allows(destination):
                raise ApplicationNetworkDenied("unsupported_transport" if destination is None else "destination")
            self.controller.permits.add(permit)
        return permit


class ApplicationNetworkController:
    def __init__(self):
        self.lock = threading.RLock()
        self.reload_lock = threading.Lock()
        self.revision = 0
        self.destination_policy = None
        self.permits = WeakSet()
        self.policy = ApplicationNetworkPolicy(self)

    def publish(self, generation: int, policy: DestinationPolicy):
        with self.lock:
            if generation != self.revision:
                return False
            self.destination_policy = policy
            for permit in self.permits:
                if not policy.allows(permit.destination) and not permit.revocation.done():
                    permit.revocation.set_result(None)
            return True

    def unavailable(self, generation: int):
        with self.lock:
            if generation == self.revision:
                self.destination_policy = None
                self._revoke()

    def invalidate(self):
        with self.lock:
            self.revision += 1
            self.destination_policy = None
            self._revoke()
            return self.revision

    def _revoke(self):
        for permit in self.permits:
            if not permit.revocation.done():
                permit.revocation.set_result(None)

    def load_config(self, loader, *, invalidate=False):
        with self.reload_lock:
            generation = self.invalidate() if invalidate else self.revision
            try:
                config = loader()
                requirements = config.config_layer_stack.requirements if config.config_layer_stack is not None else None
                policy = requirements.application_network_policy if requirements is not None else DestinationPolicy()
                if not self.publish(generation, policy):
                    raise ApplicationNetworkDenied("revoked")
            except Exception:
                self.unavailable(generation)
                raise
            config.application_network_policy = ApplicationNetworkPolicy(self, generation)
            return config


_owner: ApplicationNetworkController | None = None


def bind_application_network_owner(owner: ApplicationNetworkController | None):
    global _owner
    if _owner is not None and _owner is not owner:
        _owner.invalidate()
    _owner = owner


def release_application_network_owner(owner):
    global _owner
    if _owner is owner:
        _owner = None


def application_network_policy():
    return _owner.policy.for_current_generation() if _owner is not None else ApplicationNetworkPolicy()


def reload_application_config(loader, *, invalidate=False):
    return _owner.load_config(loader, invalidate=invalidate) if _owner is not None else loader()


class _ResponseStream(httpx.AsyncByteStream):
    def __init__(self, response, permit):
        self.response, self.permit = response, permit

    async def __aiter__(self):
        chunks = self.response.aiter_raw().__aiter__()
        try:
            while True:
                try:
                    chunk = await self.permit.run(lambda: anext(chunks))
                except StopAsyncIteration:
                    return
                yield chunk
        finally:
            await self.response.aclose()

    async def aclose(self):
        await self.response.aclose()


class ApplicationResponse(httpx.Response):
    def __init__(self, response, permit):
        self.permit = permit
        super().__init__(response.status_code, headers=response.headers, request=response.request,
            stream=_ResponseStream(response, permit), extensions=response.extensions, history=response.history)
        self.next_request = response.next_request
        if response.is_stream_consumed:
            # Auth recovery and in-memory HTTP transports may already own the
            # decoded body. Preserve it; applying Content-Encoding twice is wrong.
            self._content = response.content
            self.is_stream_consumed = True
            self.is_closed = response.is_closed

    @property
    def content(self):
        self.permit.check()
        return super().content

    @property
    def text(self):
        self.permit.check()
        return super().text

    def json(self, **kwargs):
        self.permit.check()
        return super().json(**kwargs)

    def raise_for_status(self):
        self.permit.check()
        return super().raise_for_status()

    async def aread(self):
        self.permit.check()
        return await self.permit.run(super().aread)

    async def aiter_bytes(self, chunk_size=None):
        chunks = super().aiter_bytes(chunk_size).__aiter__()
        while True:
            try:
                yield await self.permit.run(lambda: anext(chunks))
            except StopAsyncIteration:
                return


class ApplicationHttpClient(httpx.AsyncClient):
    """Check each URL before lazily resolving its proxy/transport route."""
    def __init__(self, policy, factory, *, owns_clients=True, **kwargs):
        super().__init__(trust_env=False, **kwargs)
        self.policy, self.factory = policy, factory
        self.owns_clients = owns_clients
        self.routes = {}

    async def send(self, request, *, stream=False, auth=httpx.USE_CLIENT_DEFAULT, follow_redirects=httpx.USE_CLIENT_DEFAULT):
        follows = self.follow_redirects if follow_redirects is httpx.USE_CLIENT_DEFAULT else follow_redirects
        history = []
        while True:
            permit = self.policy.acquire(str(request.url))
            route = (request.url.scheme, request.url.netloc)
            # factory owns proxy resolution, so it must remain inside admission.
            if route not in self.routes:
                self.routes[route] = self.factory(str(request.url))
            client = self.routes[route]
            raw = await permit.run(lambda: client.send(request, stream=True, auth=auth, follow_redirects=False))
            response = ApplicationResponse(raw, permit)
            response.history = list(history)
            if follows and response.next_request is not None:
                await response.aread()
                await response.aclose()
                history.append(response)
                if len(history) > self.max_redirects:
                    raise httpx.TooManyRedirects("Exceeded maximum allowed redirects", request=request)
                request = response.next_request
                continue
            if not stream:
                try:
                    await response.aread()
                except BaseException:
                    await response.aclose()
                    raise
            return response

    async def aclose(self):
        if self.owns_clients:
            await asyncio.gather(*(client.aclose() for client in set(self.routes.values())))
        await super().aclose()


def application_http_client(url, *, policy=None, proxy_mode="inherit", **kwargs):
    from backend.llm.proxy_policy import provider_httpx_proxy_kwargs
    policy = policy if policy is not None else application_network_policy()
    def create(destination):
        return httpx.AsyncClient(**kwargs, **provider_httpx_proxy_kwargs(destination, proxy_mode=proxy_mode))
    return ApplicationHttpClient(policy, create, **kwargs) if policy.managed else create(url)


class ApplicationWebSocket:
    def __init__(self, socket, permit):
        self.socket, self.permit = socket, permit
        # websockets owns an autonomous ping task even while a pooled socket is
        # idle. Revocation must close that original transport immediately.
        self.revocation_task = asyncio.create_task(self._close_when_revoked())

    async def _close_when_revoked(self):
        await self.permit.revoked()
        await self.socket.close()

    def __getattr__(self, name):
        return getattr(self.socket, name)

    async def _run(self, operation):
        try:
            return await self.permit.run(operation)
        except ApplicationNetworkDenied:
            await self.close()
            raise

    async def send(self, message, **kwargs):
        return await self._run(lambda: self.socket.send(message, **kwargs))

    async def recv(self, **kwargs):
        return await self._run(lambda: self.socket.recv(**kwargs))

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self._run(lambda: anext(self.socket.__aiter__()))

    async def close(self, **kwargs):
        self.revocation_task.cancel()
        try:
            await self.revocation_task
        except asyncio.CancelledError:
            pass
        await self.socket.close(**kwargs)


async def application_websocket_connect(url, connector, *, policy, proxy_resolver, **kwargs):
    permit = policy.acquire(url)
    if policy.managed and (not isinstance(connector, type) or not issubclass(connector, WebSocketConnect)):
        raise ApplicationNetworkDenied("unsupported_transport")
    final_permit = permit
    async def open_connection():
        opening = connector(url, proxy=proxy_resolver(url), **kwargs)
        if policy.managed:
            # Python's connector follows upgrade redirects itself. Admit each
            # actual URI before its proxy/DNS/TCP work and guard that handshake.
            create = opening.create_connection
            async def create_connection():
                nonlocal final_permit
                hop = policy.acquire(opening.uri)
                opening.proxy = proxy_resolver(opening.uri)
                connection = await hop.run(create)
                handshake = connection.handshake
                async def admitted_handshake(*args, **options):
                    return await hop.run(lambda: handshake(*args, **options))
                connection.handshake = admitted_handshake
                final_permit = hop
                return connection
            opening.create_connection = create_connection
        return await opening
    socket = await permit.run(open_connection)
    return ApplicationWebSocket(socket, final_permit) if policy.managed else socket
