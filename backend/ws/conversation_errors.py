from __future__ import annotations

from typing import TYPE_CHECKING

from backend.agent.message import AgentEvent

if TYPE_CHECKING:
    from backend.ws.handler import WebSocketSession


CONVERSATION_NOT_FOUND_ERROR_CODE = "conversation.not_found"


def conversation_not_found_event(conversation_id: str) -> AgentEvent:
    return AgentEvent(
        type="error",
        data={
            "message": f"Conversation '{conversation_id}' not found",
            "recoverable": True,
            "error_type": "conversation",
            "error_code": CONVERSATION_NOT_FOUND_ERROR_CODE,
            "conversation_id": conversation_id,
        },
    )


async def emit_conversation_not_found(
    session: "WebSocketSession",
    conversation_id: str,
    *,
    sync_list: bool = True,
) -> None:
    await session._send_event(conversation_not_found_event(conversation_id))
    if not sync_list:
        return

    active_id = str(getattr(session, "active_conversation_id", "") or "").strip()
    if active_id and session.conversation_repo.get_conversation(active_id) is None:
        session.active_conversation_id = None
        clear_runtime = getattr(session, "_clear_workspace_runtime", None)
        if callable(clear_runtime):
            clear_runtime()
    await session._send_conversation_list()
