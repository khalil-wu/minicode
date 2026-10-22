"""Publish committed sidebar and resource metadata through the existing inventory."""
from backend.api import _state
from backend.async_cleanup import to_thread_cancel_safe


async def publish_thread_metadata(repository):
    for session in _state.ws_manager.iter_sessions():
        if session.is_connected and session.conversation_repo.storage_root == repository.storage_root:
            await session.send_conversation_list()


async def publish_project_metadata(payload=None, *, sessions=None):
    from backend.services.workspace_service import list_workspace_recent_payload

    if payload is None:
        payload = await to_thread_cancel_safe(list_workspace_recent_payload)
    for session in _state.ws_manager.iter_sessions() if sessions is None else sessions:
        if session.is_connected:
            await session.send_payload(payload, log_context="projects.updated")
    return payload
