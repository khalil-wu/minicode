"""MCP SDK HTTP transport over one captured executor HTTP capability."""
from __future__ import annotations

from collections.abc import AsyncIterator, Callable

import httpx

from backend.environments import HttpHeader, HttpRequestParams


class _ExecutorResponseBody(httpx.AsyncByteStream):
    def __init__(self, initial: bytes, stream):
        self.initial = initial
        self.stream = stream

    async def __aiter__(self) -> AsyncIterator[bytes]:
        if self.initial:
            yield self.initial
        async for chunk in self.stream:
            yield chunk

    async def aclose(self) -> None:
        await self.stream.aclose()


class ExecutorMCPHTTPTransport(httpx.AsyncBaseTransport):
    """Keep SDK framing/auth/cookies while routing through the original lease."""

    def __init__(self, lease, *, bearer_token_env_var: str | None = None,
        redirect_policy: str = "follow"):
        self.lease = lease.clone()
        self.bearer_token_env_var = bearer_token_env_var
        self.redirect_policy = redirect_policy

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        headers = tuple(HttpHeader(name.decode("ascii"), value.decode("latin-1"))
            for name, value in request.headers.raw)
        if self.bearer_token_env_var is not None:
            headers = tuple(header for header in headers if header.name.casefold() != "authorization") + (
                HttpHeader("authorization", "Bearer ", self.bearer_token_env_var),)
        timeout = request.extensions.get("timeout", {}).get("read")
        params = HttpRequestParams(method=request.method, url=str(request.url), headers=headers,
            body=await request.aread(), timeout_ms=int(timeout * 1000) if timeout is not None else None,
            redirect_policy=self.redirect_policy)
        response, stream = await self.lease.environment.http.http_request_stream(params)
        return httpx.Response(response.status,
            headers=[(header.name.encode("ascii"), header.value.encode("latin-1")) for header in response.headers],
            stream=_ExecutorResponseBody(response.body, stream), request=request)

    async def aclose(self) -> None:
        await self.lease.aclose()


def executor_mcp_http_client_factory(lease, *, bearer_token_env_var: str | None = None,
    redirect_policy: str = "follow") -> Callable[..., httpx.AsyncClient]:
    """The MCP SDK owns the returned client and each request's protocol state."""
    def create_client(*, headers=None, timeout=None, auth=None):
        return httpx.AsyncClient(headers=headers, timeout=timeout, auth=auth, trust_env=False,
            follow_redirects=False, transport=ExecutorMCPHTTPTransport(lease,
                bearer_token_env_var=bearer_token_env_var, redirect_policy=redirect_policy))
    return create_client
