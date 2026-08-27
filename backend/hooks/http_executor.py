from __future__ import annotations

import asyncio
import ipaddress
import os
import re
import socket
import ssl
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlsplit

import httpcore
import httpx


_ENV_REFERENCE_RE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}|\$([A-Z_][A-Z0-9_]*)")


@dataclass(frozen=True)
class HttpHookResponse:
    ok: bool
    body: str = ""
    status_code: int | None = None
    error: str = ""
    aborted: bool = False


def url_matches_pattern(url: str, pattern: str) -> bool:
    expression = re.escape(pattern).replace(r"\*", ".*")
    return re.fullmatch(expression, url) is not None


def interpolate_header_value(
    value: str,
    *,
    allowed_env_vars: set[str],
) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1) or match.group(2) or ""
        return os.environ.get(name, "") if name in allowed_env_vars else ""

    return _ENV_REFERENCE_RE.sub(replace, value).replace("\r", "").replace("\n", "").replace("\x00", "")


async def execute_http_hook(
    *,
    url: str,
    json_input: str,
    headers: Mapping[str, str] | None,
    hook_allowed_env_vars: tuple[str, ...],
    policy_allowed_urls: tuple[str, ...] | None,
    policy_allowed_env_vars: tuple[str, ...] | None,
    timeout: float,
    sandbox_policy: Any | None = None,
) -> HttpHookResponse:
    if sandbox_policy is not None and not bool(
        getattr(sandbox_policy, "allow_network", False)
    ):
        return HttpHookResponse(
            False,
            status_code=126,
            error="HTTP hook blocked by the active turn sandbox network policy",
        )
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return HttpHookResponse(False, error=f"HTTP hook URL must use http or https: {url}")
    if policy_allowed_urls is not None and not any(
        url_matches_pattern(url, pattern) for pattern in policy_allowed_urls
    ):
        return HttpHookResponse(
            False,
            error=(
                f"HTTP hook blocked: {url} does not match any pattern in "
                "allowed_http_hook_urls"
            ),
        )

    effective_env = set(hook_allowed_env_vars)
    if policy_allowed_env_vars is not None:
        effective_env.intersection_update(policy_allowed_env_vars)
    request_headers = {"Content-Type": "application/json"}
    for name, value in (headers or {}).items():
        request_headers[str(name)] = interpolate_header_value(
            str(value),
            allowed_env_vars=effective_env,
        )

    proxy_url = _proxy_for_url(url)
    transport: httpx.AsyncBaseTransport
    if proxy_url:
        # The proxy owns target DNS in this path. The
        # target-IP guard therefore does not accidentally reject an internal
        # corporate proxy address.
        transport = httpx.AsyncHTTPTransport(proxy=proxy_url, trust_env=False)
    else:
        transport = _GuardedAsyncHTTPTransport()

    try:
        async with httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(timeout),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = await client.post(
                url,
                content=json_input.encode("utf-8"),
                headers=request_headers,
            )
    except asyncio.CancelledError:
        raise
    except (httpx.TimeoutException, TimeoutError):
        return HttpHookResponse(False, error=f"HTTP hook timed out after {timeout}s", aborted=True)
    except Exception as exc:
        return HttpHookResponse(False, error=f"HTTP hook request failed: {exc}")

    return HttpHookResponse(
        200 <= response.status_code < 300,
        body=response.text,
        status_code=response.status_code,
        error=(
            ""
            if 200 <= response.status_code < 300
            else f"HTTP {response.status_code} from {url}"
        ),
    )


class _GuardedAsyncHTTPTransport(httpx.AsyncHTTPTransport):
    def __init__(self) -> None:
        super().__init__(trust_env=False, http1=True, http2=False, retries=0)
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl.create_default_context(),
            max_connections=20,
            max_keepalive_connections=0,
            http1=True,
            http2=False,
            retries=0,
            network_backend=_SSRFGuardedNetworkBackend(),
        )


class _SSRFGuardedNetworkBackend(httpcore.AsyncNetworkBackend):
    def __init__(self) -> None:
        self._backend = httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options=None,
    ):
        addresses = await _resolve_safe_addresses(host, port)
        last_error: BaseException | None = None
        for address in addresses:
            try:
                return await self._backend.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except BaseException as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        raise httpcore.ConnectError(f"No safe address resolved for {host}")

    async def connect_unix_socket(self, path: str, timeout: float | None = None, socket_options=None):
        return await self._backend.connect_unix_socket(
            path,
            timeout=timeout,
            socket_options=socket_options,
        )

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


async def _resolve_safe_addresses(host: str, port: int) -> tuple[str, ...]:
    try:
        literal = ipaddress.ip_address(host.strip("[]"))
        addresses = [literal]
    except ValueError:
        records = await asyncio.to_thread(
            socket.getaddrinfo,
            host,
            port,
            type=socket.SOCK_STREAM,
        )
        addresses = []
        for record in records:
            value = ipaddress.ip_address(record[4][0])
            if value not in addresses:
                addresses.append(value)
    if not addresses:
        raise httpcore.ConnectError(f"DNS returned no addresses for {host}")
    forbidden = [address for address in addresses if _forbidden_target(address)]
    if forbidden:
        values = ", ".join(str(address) for address in forbidden)
        raise httpcore.ConnectError(f"HTTP hook target resolves to a blocked address: {values}")
    return tuple(str(address) for address in addresses)


def _forbidden_target(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if address.is_loopback:
        return False
    return any(
        (
            address.is_private,
            address.is_link_local,
            address.is_multicast,
            address.is_reserved,
            address.is_unspecified,
        )
    )


def _proxy_for_url(url: str) -> str | None:
    parsed = urlsplit(url)
    # Honor environment proxies only. urllib's getproxies() on
    # Windows merges WinInet registry settings, and proxy_bypass() consults
    # the registry too — a machine-wide proxy would then route hook fetches
    # outside the SSRF-guarded transport without any env opt-in.
    proxies = urllib.request.getproxies_environment()
    no_proxy = (
        os.environ.get("NO_PROXY")
        or os.environ.get("no_proxy")
        or str(proxies.get("no") or proxies.get("no_proxy") or "")
    )
    if _host_matches_no_proxy(parsed.hostname, parsed.port, no_proxy):
        return None
    value = proxies.get(parsed.scheme) or proxies.get("all")
    return str(value) if value else None


def _host_matches_no_proxy(host: str, port: int | None, raw: str) -> bool:
    """Implement the environment NO_PROXY contract without registry lookup."""
    host = host.strip("[]").rstrip(".").lower()
    if not host or not raw:
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    for token in raw.split(","):
        item = token.strip().lower()
        if not item:
            continue
        if item == "*":
            return True
        if item.startswith("."):
            item = item[1:]
        token_host, sep, token_port = item.rpartition(":")
        if sep and token_host and token_port.isdigit():
            if port is None or int(token_port) != port:
                continue
            item = token_host
        try:
            if address is not None and address in ipaddress.ip_network(item, strict=False):
                return True
        except ValueError:
            pass
        if item == host or host.endswith("." + item):
            return True
    return False
