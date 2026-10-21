"""Publish committed sidebar and resource metadata through the existing inventory."""
from backend.api import _state


async def publish_thread_metadata(repository):
    for session in _state.ws_manager.iter_sessions():
        if session.is_connected and session.conversation_repo.storage_root == repository.storage_root:
            await session.send_conversation_list()
