from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
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


def _ip_is_private_or_local(ip_value: str) -> tuple[bool, bool]:
    try:
        ip = ipaddress.ip_address(ip_value)
    except ValueError:
        return False, False
    is_local = ip.is_loopback or ip.is_link_local
    is_private = ip.is_private or ip.is_reserved or ip.is_multicast
    return is_private, is_local


def assess_network_url(url: str, *, resolve_dns: bool = True) -> NetworkTargetAssessment:
    parsed = urlparse(str(url or "").strip())
    protocol = parsed.scheme.lower()
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if protocol not in {"http", "https"}:
        return NetworkTargetAssessment(False, "URL must use http or https", host=host, protocol=protocol)
    if not host:
        return NetworkTargetAssessment(False, "URL host is required", protocol=protocol)

    if host in _LOCAL_HOSTS or host.endswith(".localhost"):
        return NetworkTargetAssessment(
            False,
            "Network target resolves to localhost",
            host=host,
            protocol=protocol,
            is_local=True,
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

    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if protocol == "https" else 80), type=socket.SOCK_STREAM)
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
