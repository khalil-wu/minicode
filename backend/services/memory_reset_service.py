"""The existing global reset domain, addressed without selecting a chat."""
from __future__ import annotations

from dataclasses import asdict

from backend.async_cleanup import to_thread_cancel_safe


async def reset_host_memory(*, confirmed: bool):
    from backend.api import _state
    from backend.agent.runtime import default_runtime_if_initialized
    from backend.conversations.repository import ConversationRepository
    from backend.memory.generation import begin_memory_reset, end_memory_reset
    from backend.ws.handlers.conversation import _memory_reset_active_conversation_ids

    if not confirmed:
        raise ValueError("Explicit confirmation is required before clearing memory.")
    sessions = _state.ws_manager.resource_sessions()
    if sessions:
        active = _memory_reset_active_conversation_ids(sessions[0])
    else:
        runtime = default_runtime_if_initialized()
        snapshot = runtime.list_runs(include_subagents=True) if runtime is not None else {"runs": [], "subagents": []}
        active = any(run["status"] == "running" for run in snapshot["runs"]) or any(
            child["status"] == "running" or child.get("background_task") in {"queued", "running"} for child in snapshot["subagents"])
    if active:
        raise ValueError("Stop active agents before clearing memory.")
    pending = await begin_memory_reset(timeout=5.0)
    if pending is None:
        raise ValueError("Memory maintenance is already stopping or resetting.")
    try:
        if pending:
            raise ValueError("Memory maintenance did not stop; nothing was reset.")
        files = await to_thread_cancel_safe(_state.bootstrap.memory_manager.reset)
        repository = ConversationRepository()
        records = await to_thread_cancel_safe(repository.reset_memory_state)
        for session in sessions:
            if session.active_conversation_id:
                conversation = repository.get_conversation(session.active_conversation_id)
                if conversation is not None:
                    session.load_active_conversation_snapshot(conversation.id, conversation.context_snapshot, snapshot_revision=conversation.revision)
            await session.send_conversation_list()
        return {**asdict(files), **records}
    finally:
        end_memory_reset()
