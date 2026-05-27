"""Health checks for live preview URLs."""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Any


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
    timeout: float = 30.0,
    interval: float = 1.0,
) -> PreviewVerification:
    """Poll until the server responds with status < 500, or timeout."""
    import asyncio

    deadline = time.perf_counter() + timeout
    last_result: PreviewVerification | None = None
    while time.perf_counter() < deadline:
        last_result = await verify_preview_url(url, timeout=min(5.0, deadline - time.perf_counter()))
        if last_result.ok:
            return last_result
        await asyncio.sleep(interval)
    return last_result or PreviewVerification(
        url=url, ok=False, status_code=None, elapsed_ms=int(timeout * 1000), error="Timeout waiting for server"
    )


async def verify_preview_url(url: str, timeout: float = 8.0) -> PreviewVerification:
    started = time.perf_counter()
    if not (url.startswith("http://") or url.startswith("https://")):
        return PreviewVerification(
            url=url,
            ok=False,
            status_code=None,
            elapsed_ms=0,
            error="Only http(s) preview URLs can be verified",
        )
    try:
        import httpx

        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            response = await client.get(url)
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
