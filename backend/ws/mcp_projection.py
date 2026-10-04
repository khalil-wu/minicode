"""Project one MCP manager only while the receiving session still owns it."""
from __future__ import annotations

from typing import Any


async def send_mcp_projection(session: Any, manager: Any, payload: dict[str, Any]) -> None:
    if session.mcp_manager is not manager:
        return
    await session.send_payload({
        **payload,
        "conversation_id": str(session.active_conversation_id or ""),
        "workspace_root": str(manager.workspace_root or "") if manager is not None else "",
    }, log_context=str(payload["type"]))
