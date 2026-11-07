"""Version-owned memory read and readiness projections."""
from __future__ import annotations

from backend.async_cleanup import to_thread_cancel_safe
from backend.config import load_config
from backend.memory.file_memory import FileMemory
from backend.memory.job_store import MEMORY_DB_NAME, MemoryJobStore
from backend.memory.prompts import valid_v2_summary
from backend.memory.settings import MemoryVersion


async def thread_memory_read(session, thread_id: str) -> dict:
    conversation = await to_thread_cancel_safe(session.conversation_repo.get_conversation_summary, thread_id)
    if conversation is None:
        raise ValueError("The requested memory thread does not exist")
    workspace = session.session_lifecycle.workspace_root_for_conversation(conversation)
    config = await to_thread_cancel_safe(load_config, cwd=workspace, resolve_credentials=False)
    settings = config.memories
    if session.ws_manager is not None:
        store_id = await to_thread_cancel_safe(session.conversation_repo.store_instance_id)
        thread = session.ws_manager.execution_contexts.thread_runtime(store_id, thread_id)
        settings = thread.capture_memory_settings(settings)
    if not config.feature_flags.enabled("memories") or not settings.use_memories:
        return {"summary": None}

    def read():
        return FileMemory.for_workspace(workspace, version=settings.version, initialize=False).read_summary()
    return {"summary": await to_thread_cancel_safe(read)}


async def memory_status(session, *, minimum: int = 20) -> dict:
    if isinstance(minimum, bool) or not isinstance(minimum, int) or not 1 <= minimum <= 4096:
        raise ValueError("minConsolidatedThreads must be between 1 and 4096")
    owner = session.active_conversation_id
    conversation = await to_thread_cancel_safe(session.conversation_repo.get_conversation_summary, owner) if owner else None
    workspace = session.session_lifecycle.workspace_root_for_conversation(conversation) if conversation is not None else None

    def read():
        memory = FileMemory.for_workspace(workspace, version=MemoryVersion.V2, initialize=False)
        count = MemoryJobStore(memory.memory_dir / MEMORY_DB_NAME,
            reset_lock=memory.reset_lock, version=MemoryVersion.V2).max_consolidated_thread_count()
        summary = memory.read_summary()
        has_v1 = FileMemory.workspace_memory_dir(workspace).exists()
        return {"v2ConsolidatedThreads": count,
            "v2Ready": not has_v1 or count >= minimum and summary is not None and valid_v2_summary(summary)}
    return await to_thread_cancel_safe(read)
