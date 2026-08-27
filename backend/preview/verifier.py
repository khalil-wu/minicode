"""Health checks for live preview URLs."""
from __future__ import annotations

import asyncio
import time
from dataclasses import asdict, dataclass
import ipaddress
from typing import Any
from urllib.parse import urljoin, urlparse

from backend.permissions.network import (
    actual_peer_network_error,
    assess_network_url,
    connected_peer_ip,
    snapshot_response_extensions,
)


@dataclass(frozen=True)
class PreviewVerification:
    url: str
    ok: bool
    status_code: int | None
    elapsed_ms: int
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class _PreviewResponseSnapshot:
    status_code: int
    url: str
    headers: Any
    extensions: dict[str, Any]


async def _get_preview_response(client: Any, url: str) -> Any:
    """Capture the connected peer before httpcore closes a streamed socket."""

    stream_method = getattr(client, "stream", None)
    if callable(stream_method):
        async with stream_method("GET", url) as response:
            return _PreviewResponseSnapshot(
                status_code=int(response.status_code),
                url=str(getattr(response, "url", url) or url),
                headers=response.headers,
                extensions=snapshot_response_extensions(response),
            )
    return await client.get(url)


async def wait_until_ready(
    url: str,
    *,
    timeout: float | None = None,
    interval: float | None = None,
) -> PreviewVerification:
    """Poll until the preview responds or the caller's deadline expires."""
    if timeout is None:
        return await verify_preview_url(url, timeout=None)

    total_timeout = max(0.0, float(timeout))
    poll_interval = max(0.0, float(interval)) if interval is not None else 1.0
    started = time.monotonic()
    last = await verify_preview_url(url, timeout=total_timeout)
    while not last.ok:
        remaining = total_timeout - (time.monotonic() - started)
        if remaining <= 0:
            return last
        await asyncio.sleep(min(poll_interval, remaining))
        remaining = total_timeout - (time.monotonic() - started)
        if remaining <= 0:
            return last
        last = await verify_preview_url(url, timeout=remaining)
    return last


async def verify_preview_url(url: str, timeout: float | None = None) -> PreviewVerification:
    started = time.perf_counter()
    allowed, reason = await asyncio.to_thread(_preview_target_allowed, url)
    if not allowed:
        return PreviewVerification(
            url=url,
            ok=False,
            status_code=None,
            elapsed_ms=0,
            error=reason,
        )
    try:
        import httpx

        request_timeout = 10.0 if timeout is None else max(0.1, float(timeout))
        async with httpx.AsyncClient(
            timeout=request_timeout,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            current = url
            response = None
            visited: set[str] = set()
            redirects = 0
            while True:
                if current in visited:
                    raise RuntimeError("Preview redirect loop detected")
                visited.add(current)
                # Re-resolve immediately before every network operation.  This
                # does not replace a socket-level IP pin, but it closes the
                # common stale-assessment path and applies the same policy to
                # every redirect hop instead of trusting the first DNS result.
                allowed, reason = await asyncio.to_thread(_preview_target_allowed, current)
                if not allowed:
                    raise RuntimeError(reason)
                response = await _get_preview_response(client, current)
                peer_error = _response_peer_network_error(response, current)
                if peer_error:
                    raise RuntimeError(peer_error)
                response_url = str(getattr(response, "url", current) or current)
                allowed, reason = await asyncio.to_thread(
                    _preview_target_allowed,
                    response_url,
                )
                if not allowed:
                    raise RuntimeError(reason)
                if response.status_code not in {301, 302, 303, 307, 308}:
                    break
                if redirects >= client.max_redirects:
                    raise RuntimeError("Too many preview redirects")
                location = response.headers.get("location")
                if not location:
                    break
                redirect_url = urljoin(current, location)
                if _url_origin(redirect_url) != _url_origin(current):
                    raise RuntimeError("Preview verification will not follow a cross-origin redirect")
                current = redirect_url
                redirects += 1
            if response is None:
                raise RuntimeError("Preview verification returned no response")
        elapsed = int((time.perf_counter() - started) * 1000)
        return PreviewVerification(
            url=url,
            ok=response.status_code < 500,
            status_code=response.status_code,
            elapsed_ms=elapsed,
            error="" if response.status_code < 500 else f"HTTP {response.status_code}",
        )
    except Exception as exc:
        elapsed = int((time.perf_counter() - started) * 1000)
        error = str(exc) or exc.__class__.__name__
        return PreviewVerification(
            url=url,
            ok=False,
            status_code=None,
            elapsed_ms=elapsed,
            error=error,
        )


def _preview_target_allowed(url: str) -> tuple[bool, str]:
    parsed = urlparse(str(url or "").strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return False, "Only http(s) preview URLs with a host can be verified"
    try:
        parsed.port
    except ValueError:
        return False, "Preview URL port is invalid"
    host = parsed.hostname.strip().lower().rstrip(".")
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".localhost"):
        return True, ""
    try:
        if ipaddress.ip_address(host).is_loopback:
            return True, ""
    except ValueError:
        pass
    assessment = assess_network_url(url)
    if assessment.allowed:
        return True, ""
    return False, f"Preview target is blocked: {assessment.reason}"


def _url_origin(url: str) -> tuple[str, str, int] | None:
    parsed = urlparse(str(url or "").strip())
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if scheme not in {"http", "https"} or not host:
        return None
    try:
        port = parsed.port or (443 if scheme == "https" else 80)
    except ValueError:
        return None
    return scheme, host, int(port)


def _response_peer_network_error(response: Any, target_url: str) -> str:
    """Detect DNS rebinding after httpx has opened the socket.

    Localhost/loopback previews are intentional and remain valid.  For a
    public hostname, a direct peer in loopback/private/link-local/reserved
    space is rejected even when the preflight DNS lookup was public.
    """

    parsed = urlparse(str(target_url or "").strip())
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".localhost"):
        return ""
    try:
        if ipaddress.ip_address(host).is_loopback:
            return ""
    except ValueError:
        pass
    error = actual_peer_network_error(response, target_url)
    if error.startswith("Network connection resolved a public hostname"):
        peer_ip = connected_peer_ip(response) or "unknown"
        return f"Preview connection resolved a public hostname to a local or private peer ({peer_ip})"
    if error.startswith("Network connection peer could not be verified"):
        return "Preview connection peer could not be verified (transport hid the socket peer)"
    return error
