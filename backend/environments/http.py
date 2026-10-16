"""Source executor-owned HTTP requests and request-scoped body streams."""
from __future__ import annotations

import asyncio
import base64
from collections import deque
from dataclasses import dataclass

from .errors import ExecutorProtocolError
from .filesystem import decode_bytes

MAX_HTTP_BODY_DELTA_BYTES = 1024 * 1024
MAX_HTTP_BODY_FRAMES = 256
MAX_QUEUED_HTTP_BODY_BYTES = 16 * 1024 * 1024
MAX_ENCODED_HTTP_BODY_DELTA_BYTES = ((MAX_HTTP_BODY_DELTA_BYTES + 2) // 3) * 4


@dataclass(frozen=True)
class HttpHeader:
    name: str
    value: str
    value_env_var: str | None = None

    def to_wire(self) -> dict:
        result = {"name": self.name, "value": self.value}
        if self.value_env_var is not None:
            result["valueEnvVar"] = self.value_env_var
        return result


@dataclass(frozen=True)
class HttpRequestParams:
    method: str
    url: str
    headers: tuple[HttpHeader, ...] = ()
    body: bytes | None = None
    timeout_ms: int | None = None
    redirect_policy: str = "follow"
    request_id: str = ""

    def __post_init__(self):
        object.__setattr__(self, "headers", tuple(self.headers))
        if self.body is not None:
            object.__setattr__(self, "body", bytes(self.body))

    def to_wire(self, *, stream: bool, request_id: str) -> dict:
        result = {"method": self.method, "url": self.url, "headers": [header.to_wire() for header in self.headers],
                  "bodyBase64": base64.b64encode(self.body).decode("ascii") if self.body is not None else None,
                  "redirectPolicy": self.redirect_policy, "requestId": request_id, "streamResponse": stream}
        if self.timeout_ms is not None:
            result["timeoutMs"] = self.timeout_ms
        return result


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: tuple[HttpHeader, ...]
    body: bytes

    @classmethod
    def from_wire(cls, response: dict) -> HttpResponse:
        return cls(response["status"], tuple(HttpHeader(header["name"], header["value"], header.get("valueEnvVar"))
                                             for header in response["headers"]),
                   decode_bytes(response["bodyBase64"], "bodyBase64"))


@dataclass(frozen=True)
class _BodyDelta:
    seq: int
    chunk: bytes
    done: bool
    error: str | None
    queued_bytes: int


class _BodyRoute:
    """Sender-side route; it does not retain the consumer/its environment lease."""
    def __init__(self, client, rpc, request_id: str):
        self.client = client
        self.rpc = rpc
        self.request_id = request_id
        self.queue = deque()
        self.changed = asyncio.Event()
        self.finished = False
        self.failure: Exception | None = None

    def remove(self) -> None:
        if self.client.http_streams.get(self.request_id) is self:
            self.client.http_streams.pop(self.request_id)
        self.finished = True
        self.changed.set()
        self.client.notify_idle()

    def enqueue(self, delta: _BodyDelta) -> None:
        if self.client.http_queued_bytes + delta.queued_bytes > MAX_QUEUED_HTTP_BODY_BYTES:
            self.failure = ExecutorProtocolError("queued HTTP body deltas exceed 16777216 bytes")
            self.remove()
            return
        if len(self.queue) == MAX_HTTP_BODY_FRAMES:
            self.failure = ExecutorProtocolError("HTTP body delta channel filled before delivery")
            self.remove()
            return
        self.client.http_queued_bytes += delta.queued_bytes
        self.queue.append(delta)
        self.changed.set()
        if delta.done or delta.error is not None:
            self.remove()

    def fail_transport(self, error: Exception) -> None:
        # Source failure notifications wake readers even with no byte permits.
        if len(self.queue) < MAX_HTTP_BODY_FRAMES:
            self.queue.append(_BodyDelta(1, b"", True, str(error), 0))
        else:
            self.failure = error
        self.remove()

    def clear(self) -> None:
        while self.queue:
            self.client.http_queued_bytes -= self.queue.popleft().queued_bytes
        self.remove()


def handle_http_body_delta(client, rpc, params: dict) -> None:
    encoded = params["deltaBase64"]
    if len(encoded) > MAX_ENCODED_HTTP_BODY_DELTA_BYTES:
        raise ExecutorProtocolError("http response body delta exceeds 1048576 bytes")
    chunk = decode_bytes(encoded, "deltaBase64")
    if len(chunk) > MAX_HTTP_BODY_DELTA_BYTES:
        raise ExecutorProtocolError("http response body delta exceeds 1048576 bytes")
    route = client.http_streams.get(params["requestId"])
    # Late frames from a released route or an old socket have no receiver.
    if route is None or route.rpc is not rpc:
        return
    error = params.get("error")
    cost = len(chunk) + (len(error.encode("utf-8")) if error is not None else 0)
    route.enqueue(_BodyDelta(params["seq"], chunk, params.get("done", False), error, cost))


class HttpBodyStream:
    def __init__(self, route: _BodyRoute, lease):
        self.closed = False
        self.route = route
        self.lease = lease
        self.next_seq = 1
        self.pending_eof = False
        self.owner_failure: Exception | None = None
        self.close_task: asyncio.Task | None = None
        route.client.http_stream_handles.add(self)

    @property
    def request_id(self) -> str:
        return self.route.request_id

    async def recv(self) -> bytes | None:
        try:
            return await self._recv()
        except asyncio.CancelledError:
            await self.aclose()
            raise

    async def _recv(self) -> bytes | None:
        if self.owner_failure is not None:
            raise self.owner_failure
        if self.closed:
            return None
        if self.pending_eof:
            await self.aclose()
            return None
        while not self.route.queue:
            if self.route.finished:
                error = self.route.failure
                await self.aclose()
                if error is not None:
                    raise ExecutorProtocolError(f"http response stream `{self.request_id}` failed: {error}") from error
                return None
            self.route.changed.clear()
            await self.route.changed.wait()
            if self.owner_failure is not None:
                raise self.owner_failure
            if self.closed:
                return None
        delta = self.route.queue.popleft()
        self.route.client.http_queued_bytes -= delta.queued_bytes
        if delta.seq != self.next_seq:
            await self.aclose()
            raise ExecutorProtocolError(f"http response stream received seq {delta.seq}, expected {self.next_seq}")
        self.next_seq += 1
        if delta.error is not None:
            await self.aclose()
            raise ExecutorProtocolError(f"http response stream `{self.request_id}` failed: {delta.error}")
        if delta.done:
            self.route.remove()
            if not delta.chunk:
                await self.aclose()
                return None
            self.pending_eof = True
        return delta.chunk

    async def _close(self) -> None:
        self.closed = True
        self.route.clear()
        await self.lease.aclose()

    async def aclose(self) -> None:
        # Source has no http/cancel RPC. Closing revokes this local route only.
        if self.close_task is None:
            self.close_task = asyncio.create_task(self._close(), name="executor-http-body-close")
        await asyncio.shield(self.close_task)

    async def owner_close(self, error: Exception) -> None:
        if self.closed:
            return
        if self.pending_eof:
            await self.aclose()
            return
        self.owner_failure = error
        await self.aclose()

    async def __aenter__(self) -> HttpBodyStream:
        return self

    async def __aexit__(self, *_args) -> None:
        await self.aclose()

    async def __aiter__(self):
        try:
            while (chunk := await self.recv()) is not None:
                yield chunk
        finally:
            await self.aclose()

    def __del__(self):
        if not self.closed:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                return  # Source Drop likewise requires the owner's live runtime.
            self.route.client.retain_cleanup(self.aclose())


class ExecutorHttpClient:
    def __init__(self, environment):
        self.environment = environment

    async def _prepare(self, params: HttpRequestParams):
        client = await self.environment.get_client()
        if any(header.value_env_var is not None for header in params.headers):
            info = client.environment_info
            if info is None:
                info = await client.info()
            if not info.get("capabilities", {}).get("httpHeaderEnvVars", False):
                raise ExecutorProtocolError("executor does not advertise httpHeaderEnvVars")
        return client, await client.get_connection()

    async def http_request(self, params: HttpRequestParams) -> HttpResponse:
        async with self.environment.borrow():
            _client, rpc = await self._prepare(params)
            response = await rpc.request("http/request", params.to_wire(stream=False, request_id=params.request_id))
            return HttpResponse.from_wire(response)

    async def http_request_stream(self, params: HttpRequestParams) -> tuple[HttpResponse, HttpBodyStream]:
        lease = self.environment.borrow()
        stream = None
        try:
            client, rpc = await self._prepare(params)
            request_id = f"http-{client.next_http_request_id}"
            client.next_http_request_id += 1
            route = _BodyRoute(client, rpc, request_id)
            stream = HttpBodyStream(route, lease)
            client.http_streams[request_id] = route
            # Register before sending: bodyDelta can precede the header reply.
            response = await rpc.request("http/request", params.to_wire(stream=True, request_id=request_id))
            return HttpResponse.from_wire(response), stream
        except BaseException:
            if stream is not None:
                await stream.aclose()
            else:
                await lease.aclose()
            raise
