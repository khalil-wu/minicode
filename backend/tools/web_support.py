"""Web fetch/search helper functions.

Extracted from ``backend/tools/web_tools.py`` so URL safety, redirect policy
and response handling helpers are independent of the tool classes.
"""

from __future__ import annotations

import logging

from backend.permissions.network import actual_peer_network_error as _network_actual_peer_network_error
from backend.permissions.network import _ip_is_private_or_local
from backend.tools.untrusted import wrap_untrusted_content
from typing import Any
from urllib.parse import urlparse
import os
import re

_wrap_untrusted_content = wrap_untrusted_content


logger = logging.getLogger(__name__)


HOSTILE_FETCH_DOMAINS = {
    "zhihu.com",
    "www.zhihu.com",
    "zhuanlan.zhihu.com",
}


WEB_FETCH_MAX_WIRE_BYTES = 10 * 1024 * 1024


class _PublicConnectionBackend:
    """Verify a direct TCP stream before httpcore can send TLS/HTTP bytes."""

    def __init__(self, backend: Any) -> None:
        self._backend = backend

    async def connect_tcp(
        self, host: str, port: int, timeout: float | None = None,
        local_address: str | None = None, socket_options: Any = None,
    ) -> Any:
        stream = await self._backend.connect_tcp(
            host, port, timeout=timeout, local_address=local_address,
            socket_options=socket_options,
        )
        try:
            peer_ip = str(stream.get_extra_info("server_addr")[0])
            private, local = _ip_is_private_or_local(peer_ip)
            if private or local:
                raise RuntimeError(
                    "Network connection resolved to a local or private peer "
                    f"({peer_ip}); rejected before sending the HTTP request"
                )
        except BaseException:
            # The pool does not own this stream until connect_tcp returns.
            await stream.aclose()
            raise
        return stream

    async def connect_unix_socket(
        self, path: str, timeout: float | None = None, socket_options: Any = None,
    ) -> Any:
        return await self._backend.connect_unix_socket(
            path, timeout=timeout, socket_options=socket_options,
        )

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


def public_http_transport() -> Any:
    """Keep httpx's URL/Host/TLS contract and bind its direct connection boundary."""
    import httpx

    transport = httpx.AsyncHTTPTransport(trust_env=False)
    transport._pool._network_backend = _PublicConnectionBackend(transport._pool._network_backend)
    return transport


def _is_hostile_fetch_url(url: str) -> bool:
    try:
        host = urlparse(url).netloc.lower()
    except ValueError:
        return False
    return host in HOSTILE_FETCH_DOMAINS or host.endswith(".zhihu.com")


def _url_has_credentials(url: str) -> bool:
    """Reject URLs embedding username:password."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return bool(parsed.username or parsed.password)


def _strip_www(hostname: str) -> str:
    return re.sub(r"^www\.", "", (hostname or "").lower())


def _normalize_domain_list(value: Any) -> list[str]:
    """Coerce an allowed/blocked_domains arg into a list of bare host suffixes."""
    if isinstance(value, str):
        raw = [part for part in re.split(r"[\s,]+", value) if part]
    elif isinstance(value, list):
        raw = [str(item) for item in value]
    else:
        raw = []
    normalized: list[str] = []
    for item in raw:
        host = item.strip().lower()
        if not host:
            continue
        # Accept bare domains or full URLs; reduce to host and strip a leading www.
        if "://" in host:
            host = urlparse(host).hostname or host
        normalized.append(_strip_www(host))
    return normalized


def _is_permitted_redirect(original_url: str, redirect_url: str) -> bool:
    """Only follow same-origin redirects.

    Permits path/query changes and www. add/remove on the SAME host; requires
    identical scheme and port and no embedded credentials. Cross-host redirects
    are returned to the model instead of followed (open-redirect / SSRF guard).
    """
    try:
        original = urlparse(original_url)
        redirect = urlparse(redirect_url)
        original_port = original.port or (443 if original.scheme == "https" else 80)
        redirect_port = redirect.port or (443 if redirect.scheme == "https" else 80)
    except ValueError:
        return False
    if redirect.scheme != original.scheme:
        return False
    if redirect_port != original_port:
        return False
    if redirect.username or redirect.password:
        return False
    return _strip_www(redirect.hostname or "") == _strip_www(original.hostname or "")


def _detect_proxy() -> str | None:
    """Detect proxy for web tools. Prefers LLM_PROXY_URL (local) over commercial proxy pool."""
    # Prefer the local proxy (LLM_PROXY_URL) for web tools — more reliable than commercial pool
    llm_proxy = os.environ.get("LLM_PROXY_URL", "").strip()
    if llm_proxy:
        return llm_proxy
    # Env vars
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        val = os.environ.get(var, "").strip()
        if val:
            return val
    # Windows registry fallback
    if os.name == "nt":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
            ) as key:
                enable, _ = winreg.QueryValueEx(key, "ProxyEnable")
                if enable:
                    server, _ = winreg.QueryValueEx(key, "ProxyServer")
                    if server and not server.startswith(("socks", "SOCKS")):
                        if "://" not in server:
                            server = f"http://{server}"
                        return server
        except Exception:
            pass
    return None


def _assert_response_length_within_limit(response: Any) -> None:
    headers = getattr(response, "headers", {}) or {}
    raw_length = headers.get("content-length") if hasattr(headers, "get") else None
    if raw_length:
        try:
            if int(raw_length) > WEB_FETCH_MAX_WIRE_BYTES:
                raise RuntimeError(
                    f"Response exceeds the {WEB_FETCH_MAX_WIRE_BYTES} byte fetch limit"
                )
        except ValueError:
            # Malformed Content-Length is not trusted; the streaming byte
            # counter remains authoritative.
            pass


async def _read_response_bytes(response: Any) -> bytes:
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        if isinstance(chunk, str):
            chunk = chunk.encode("utf-8")
        total += len(chunk)
        if total > WEB_FETCH_MAX_WIRE_BYTES:
            raise RuntimeError(
                f"Response exceeds the {WEB_FETCH_MAX_WIRE_BYTES} byte fetch limit"
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _decoded_response_headers(response: Any) -> list[tuple[str, str]]:
    """Preserve representation metadata after httpx has decoded the body.

    ``Response.aiter_bytes()`` yields decoded bytes.  Reusing the original
    Content-Encoding/Content-Length/Transfer-Encoding headers on a new
    response would make httpx treat that decoded body as compressed wire data
    a second time.  Claude Code likewise consumes axios' decoded arraybuffer
    directly instead of replaying the original transfer headers.
    """

    headers = getattr(response, "headers", {}) or {}
    multi_items = getattr(headers, "multi_items", None)
    items = multi_items() if callable(multi_items) else headers.items()
    invalid_after_decode = {
        "content-encoding",
        "content-length",
        "transfer-encoding",
    }
    return [
        (str(name), str(value))
        for name, value in items
        if str(name).strip().lower() not in invalid_after_decode
    ]


def _actual_peer_network_error(
    response: Any,
    target_url: str,
    *,
    proxy_url: str | None = None,
) -> str:
    """Keep the historical web-tools helper on the canonical network policy."""

    return _network_actual_peer_network_error(
        response,
        target_url,
        proxy_url=proxy_url,
    )



