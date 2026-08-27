from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse


@dataclass(frozen=True)
class NetworkTargetAssessment:
    allowed: bool
    reason: str = ""
    host: str = ""
    protocol: str = ""
    is_local: bool = False
    is_private: bool = False
    dns_failed: bool = False


_LOCAL_HOSTS = {"localhost", "localhost.localdomain"}
_CONNECTED_PEER_IP_EXTENSION = "minicode.connected_peer_ip"


def snapshot_response_extensions(response: Any) -> dict[str, Any]:
    """Copy response extensions while the socket is open and pin its peer IP.

    httpcore's ``network_stream`` metadata is live rather than immutable. Once
    a response body closes the connection, ``get_extra_info('server_addr')``
    returns ``None``. Security checks that run on a bounded/cloned response
    therefore need a stable peer snapshot captured before reading or closing
    the body.
    """

    raw_extensions = getattr(response, "extensions", None)
    extensions = dict(raw_extensions) if isinstance(raw_extensions, dict) else {}
    peer_ip = connected_peer_ip(response)
    if peer_ip:
        extensions[_CONNECTED_PEER_IP_EXTENSION] = peer_ip
    return extensions


def connected_peer_ip(response: Any) -> str | None:
    """Return the actual socket peer recorded by httpx/httpcore.

    httpcore's built-in asyncio/AnyIO, Trio, and sync backends expose the
    remote endpoint as ``server_addr``.  Some injected transports use the
    socket-style ``peername`` spelling instead, so accept both and finally
    consult the raw socket when available.  Keeping this adapter here avoids
    security checks silently diverging between web fetch and preview health
    verification.
    """

    extensions = getattr(response, "extensions", None)
    if not isinstance(extensions, dict):
        return None
    snapshotted = extensions.get(_CONNECTED_PEER_IP_EXTENSION)
    if isinstance(snapshotted, str):
        candidate = snapshotted.strip().strip("[]").split("%", 1)[0]
        try:
            return str(ipaddress.ip_address(candidate))
        except ValueError:
            pass
    stream = extensions.get("network_stream")
    getter = getattr(stream, "get_extra_info", None)
    if not callable(getter):
        return None

    candidates: list[Any] = []
    for key in ("server_addr", "peername"):
        try:
            candidates.append(getter(key))
        except Exception:
            continue
    try:
        raw_socket = getter("socket")
        socket_peer = getattr(raw_socket, "getpeername", None)
        if callable(socket_peer):
            candidates.append(socket_peer())
    except Exception:
        pass

    for peer in candidates:
        if isinstance(peer, (tuple, list)) and peer:
            peer = peer[0]
        if not isinstance(peer, str):
            continue
        candidate = peer.strip().strip("[]").split("%", 1)[0]
        try:
            return str(ipaddress.ip_address(candidate))
        except ValueError:
            continue
    return None


def actual_peer_network_error(
    response: Any,
    target_url: str,
    *,
    proxy_url: str | None = None,
) -> str:
    """Reject a public hostname whose direct socket landed on a private IP.

    A proxy transport exposes the proxy as the socket peer, not the origin, so
    peer validation is intentionally skipped when an explicit proxy is used.
    The caller must still run :func:`assess_network_url` before every request.
    """

    if proxy_url:
        return ""
    peer_ip = connected_peer_ip(response)
    if not peer_ip:
        return "Network connection peer could not be verified (transport hid the socket peer)"
    try:
        target_host = (urlparse(target_url).hostname or "").strip().lower().rstrip(".")
        ipaddress.ip_address(target_host)
    except ValueError:
        pass
    else:
        # Literal targets are classified by assess_network_url before the
        # request, so this check only protects hostname-to-address rebinding.
        return ""
    peer_private, peer_local = _ip_is_private_or_local(peer_ip)
    if peer_private or peer_local:
        return (
            "Network connection resolved a public hostname to a local or private "
            f"peer ({peer_ip})"
        )
    return ""


_CGNAT_NETWORK = ipaddress.ip_network("100.64.0.0/10")


def _ip_is_private_or_local(ip_value: str) -> tuple[bool, bool]:
    try:
        ip = ipaddress.ip_address(ip_value)
    except ValueError:
        return False, False
    is_local = ip.is_loopback or ip.is_link_local
    # Python < 3.13 excludes CGNAT 100.64.0.0/10 from is_private; cc's
    # ssrfGuard blocks it explicitly, so carrier-grade NAT peers must too.
    is_private = (
        ip.is_private
        or ip.is_reserved
        or ip.is_multicast
        or ip in _CGNAT_NETWORK
    )
    return is_private, is_local


def assess_network_url(url: str, *, resolve_dns: bool = True) -> NetworkTargetAssessment:
    try:
        parsed = urlparse(str(url or "").strip())
    except ValueError:
        return NetworkTargetAssessment(False, "URL is malformed")
    protocol = parsed.scheme.lower()
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if protocol not in {"http", "https"}:
        return NetworkTargetAssessment(False, "URL must use http or https", host=host, protocol=protocol)
    if not host:
        return NetworkTargetAssessment(False, "URL host is required", protocol=protocol)
    if parsed.username or parsed.password:
        return NetworkTargetAssessment(
            False,
            "URL must not contain embedded credentials",
            host=host,
            protocol=protocol,
        )

    if host in _LOCAL_HOSTS or host.endswith(".localhost"):
        return NetworkTargetAssessment(
            False,
            "Network target resolves to localhost",
            host=host,
            protocol=protocol,
            is_local=True,
        )

    try:
        port = parsed.port
    except ValueError:
        return NetworkTargetAssessment(
            False,
            "URL port is invalid",
            host=host,
            protocol=protocol,
        )

    private, local = _ip_is_private_or_local(host)
    if private or local:
        return NetworkTargetAssessment(
            False,
            "Network target resolves to a local or private address",
            host=host,
            protocol=protocol,
            is_local=local,
            is_private=private,
        )

    if not resolve_dns:
        return NetworkTargetAssessment(True, host=host, protocol=protocol)

    if port is None:
        port = 443 if protocol == "https" else 80
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return NetworkTargetAssessment(
            False,
            "Network target DNS lookup failed",
            host=host,
            protocol=protocol,
            dns_failed=True,
        )

    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        ip_value = str(sockaddr[0])
        private, local = _ip_is_private_or_local(ip_value)
        if private or local:
            return NetworkTargetAssessment(
                False,
                "Network target resolves to a local or private address",
                host=host,
                protocol=protocol,
                is_local=local,
                is_private=private,
            )

    return NetworkTargetAssessment(True, host=host, protocol=protocol)
