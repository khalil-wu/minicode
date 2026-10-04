"""MiniCode MCP transport contract."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse


MCP_CONFIG_TRANSPORTS = frozenset({"stdio", "sse", "http", "ws"})
MCP_REMOTE_TRANSPORTS = frozenset({"sse", "http", "ws"})


def normalize_mcp_transport(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("MCP transport must be a string")
    transport = value.strip().lower()
    if not transport:
        raise ValueError("MCP transport cannot be empty")

    if transport not in MCP_CONFIG_TRANSPORTS:
        raise ValueError(
            f"unsupported MCP transport '{transport}'; expected one of: "
            f"{', '.join(sorted(MCP_CONFIG_TRANSPORTS))}"
        )
    return transport


def mcp_transport_from_mapping(
    mapping: Mapping[str, Any],
) -> str:
    """Read the one explicit MiniCode transport field."""

    if "transport" not in mapping:
        raise ValueError("MCP config requires an explicit transport")
    if "type" in mapping:
        raise ValueError("MCP config field 'type' is not supported; use 'transport'")
    return normalize_mcp_transport(mapping.get("transport"))


def normalize_mcp_remote_url(value: Any, transport: str) -> str:
    """Validate the endpoint contract shared by config writes and runtime admission."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"MCP {transport} transport requires a URL")
    url = value.strip()
    scheme = urlparse(url).scheme.lower()
    allowed_schemes = {"ws", "wss"} if transport == "ws" else {"http", "https"}
    if scheme not in allowed_schemes:
        raise ValueError(f"invalid {transport} URL scheme '{scheme or '(missing)'}'")
    return url
