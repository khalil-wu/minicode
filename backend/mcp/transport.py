"""MiniCode MCP transport contract."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


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
