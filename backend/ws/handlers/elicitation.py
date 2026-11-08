"""Live thread elicitation counters; their lifetime follows the loaded owner."""
from functools import partial

from backend.agent.runtime import default_runtime_if_initialized
from backend.ws.command_results import emit_command_error


async def handle_counter(session, data, *, increment):
    command = "thread/increment_elicitation" if increment else "thread/decrement_elicitation"
    thread_id = str(data.get("threadId") or "")
    execution = session.ws_manager.execution_contexts.context_for(thread_id) if session.ws_manager is not None else None
    if execution is not None and not execution.shutting_down:
        store = execution.host.conversation_runtime.code_store_for(thread_id)
    else:
        runtime = default_runtime_if_initialized()
        child = runtime.subagent_session(thread_id) if runtime is not None else None
        if child is None or child.closed:
            await emit_command_error(session, command, f"Thread is not loaded: {thread_id}",
                data={"conversation_id": thread_id, "error_code": "thread_not_loaded"})
            return True
        store = child.code_store
    try:
        count = store.elicitations.increment() if increment else store.elicitations.decrement()
    except (ValueError, OverflowError) as error:
        await emit_command_error(session, command, error, data={"conversation_id": thread_id,
            "error_code": "elicitation_count_invalid"})
        return True
    await session.emit_command_result(command, "", data={"conversation_id": thread_id, "count": count, "paused": count > 0})
    return True


HANDLERS = {
    "thread/increment_elicitation": partial(handle_counter, increment=True),
    "thread/decrement_elicitation": partial(handle_counter, increment=False),
}
