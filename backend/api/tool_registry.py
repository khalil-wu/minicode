"""Compatibility exports for tool registry construction."""

from __future__ import annotations

from backend.services.tool_registry_factory import (
    build_tool_registry as _build_tool_registry,
    get_attachment_store as _get_attachment_store,
    register_mcp_tools as _register_mcp_tools,
)

__all__ = ["_build_tool_registry", "_get_attachment_store", "_register_mcp_tools"]
