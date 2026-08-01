"""Health checks for live preview URLs."""
from __future__ import annotations

import asyncio
import time
from dataclasses import asdict, dataclass
import ipaddress
from typing import Any
from urllib.parse import urljoin, urlparse

from backend.permissions.network import assess_network_url


@dataclass(frozen=True)
class PreviewVerification:
    url: str
    ok: bool
    status_code: int | None
    elapsed_ms: int
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


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

        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False) as client:
            current = url
            response = None
            visited: set[str] = set()
            redirects = 0
            while True:
                if current in visited:
                    raise RuntimeError("Preview redirect loop detected")
                visited.add(current)
                response = await client.get(current)
                if response.status_code not in {301, 302, 303, 307, 308}:
                    break
                if redirects >= client.max_redirects:
                    raise RuntimeError("Too many preview redirects")
                location = response.headers.get("location")
                if not location:
                    break
                redirect_url = urljoin(current, location)
                if urlparse(redirect_url).netloc.casefold() != urlparse(current).netloc.casefold():
                    raise RuntimeError("Preview verification will not follow a cross-origin redirect")
                allowed, reason = await asyncio.to_thread(_preview_target_allowed, redirect_url)
                if not allowed:
                    raise RuntimeError(reason)
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
