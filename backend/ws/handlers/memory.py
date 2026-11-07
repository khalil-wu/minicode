"""Source memory query contracts on the existing owned command transport."""
from backend.services.memory_read_service import memory_status, thread_memory_read


async def handle_thread_memory_read(session, data):
    thread_id = data["threadId"]
    result = await thread_memory_read(session, thread_id)
    await session.emit_command_result("thread/memory/read", "", data={"conversation_id": thread_id, **result})
    return True


async def handle_memory_status(session, data):
    result = await memory_status(session, minimum=data.get("minConsolidatedThreads", 20))
    await session.emit_command_result("memory/status", "", data=result)
    return True


HANDLERS = {"thread/memory/read": handle_thread_memory_read, "memory/status": handle_memory_status}
