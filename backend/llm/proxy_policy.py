"""Shared per-provider proxy resolution for diagnostics and runtime adapters."""

from __future__ import annotations

import os
from urllib.parse import urlparse


PROVIDER_PROXY_MODES = frozenset({"inherit", "direct"})


def normalize_provider_proxy_mode(value: object, default: str = "inherit") -> str:
    normalized_default = str(default or "inherit").strip().lower()
    if normalized_default not in PROVIDER_PROXY_MODES:
        normalized_default = "inherit"
    mode = str(value if value is not None else normalized_default).strip().lower()
    return mode if mode in PROVIDER_PROXY_MODES else normalized_default


def host_matches_no_proxy(host: str, port: int | None, no_proxy: str) -> bool:
    """Match the conventional NO_PROXY hostname and optional-port grammar."""

    normalized_host = str(host or "").strip().lower().strip("[]").rstrip(".")
    if not normalized_host:
        return False
    for raw_entry in str(no_proxy or "").split(","):
        entry = raw_entry.strip().lower()
        if not entry:
            continue
        if entry == "*":
            return True
        entry_host = entry
        entry_port: int | None = None
        if "://" in entry:
            parsed_entry = urlparse(entry)
            entry_host = str(parsed_entry.hostname or "")
            try:
                entry_port = parsed_entry.port
            except ValueError:
                continue
        elif entry.startswith("["):
            closing_bracket = entry.find("]")
            if closing_bracket < 0:
                continue
            entry_host = entry[1:closing_bracket]
            remainder = entry[closing_bracket + 1 :]
            if remainder:
                if not remainder.startswith(":") or not remainder[1:].isdigit():
                    continue
                entry_port = int(remainder[1:])
        elif entry.count(":") == 1:
            candidate_host, candidate_port = entry.rsplit(":", 1)
            if candidate_port.isdigit():
                entry_host = candidate_host
                entry_port = int(candidate_port)
        entry_host = entry_host.lstrip(".").strip("[]").rstrip(".")
        if entry_host.startswith("*."):
            entry_host = entry_host[2:]
        if not entry_host or (entry_port is not None and entry_port != port):
            continue
        if normalized_host == entry_host or normalized_host.endswith(f".{entry_host}"):
            return True
    return False


def provider_proxy_url_for_base_url(
    base_url: str,
    *,
    proxy_mode: object = "inherit",
) -> str:
    """Resolve one provider request to a proxy URL or an explicit direct path.

    Resolution is deterministic and shared by settings diagnostics and live
    adapters.  ``direct`` ignores every process proxy. ``inherit`` honors
    NO_PROXY first, then MiniCode's explicit LLM proxy, then conventional
    scheme-specific proxy variables.  The returned URL is consumed with
    ``trust_env=False`` so httpx cannot silently re-apply a bypassed proxy.
    """

    if normalize_provider_proxy_mode(proxy_mode) == "direct":
        return ""

    parsed = urlparse(str(base_url or ""))
    host = str(parsed.hostname or "").strip().lower()
    try:
        port = parsed.port
    except ValueError:
        port = None
    if port is None:
        if parsed.scheme.lower() == "https":
            port = 443
        elif parsed.scheme.lower() == "http":
            port = 80
    no_proxy = ",".join(
        value
        for value in (os.getenv("NO_PROXY", ""), os.getenv("no_proxy", ""))
        if value
    )
    if host and host_matches_no_proxy(host, port, no_proxy):
        return ""

    explicit = (
        os.getenv("LLM_PROXY_URL", "").strip()
        or os.getenv("MINICODE_LLM_PROXY_URL", "").strip()
    )
    if explicit:
        return explicit

    if parsed.scheme.lower() == "https":
        proxy = (
            os.getenv("HTTPS_PROXY", "").strip()
            or os.getenv("https_proxy", "").strip()
        )
    else:
        proxy = (
            os.getenv("HTTP_PROXY", "").strip()
            or os.getenv("http_proxy", "").strip()
        )
    return (
        proxy
        or os.getenv("ALL_PROXY", "").strip()
        or os.getenv("all_proxy", "").strip()
    )


def provider_httpx_proxy_kwargs(
    base_url: str,
    *,
    proxy_mode: object = "inherit",
) -> dict[str, object]:
    """Return explicit httpx kwargs with no implicit environment fallback."""

    proxy_url = provider_proxy_url_for_base_url(
        base_url,
        proxy_mode=proxy_mode,
    )
    return {
        "proxy": proxy_url or None,
        "trust_env": False,
    }


__all__ = [
    "PROVIDER_PROXY_MODES",
    "host_matches_no_proxy",
    "normalize_provider_proxy_mode",
    "provider_httpx_proxy_kwargs",
    "provider_proxy_url_for_base_url",
]
