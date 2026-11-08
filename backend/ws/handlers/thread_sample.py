"""Live sample request and cancellation transport."""
import logging

from backend.agent.thread_sample import SampleError, SampleParams
from backend.llm.errors import sanitize_llm_error_message
from backend.ws.command_results import emit_command_error
from backend.ws.thread_sample import request_key

logger = logging.getLogger(__name__)


async def handle_sample(session, data):
    request_id = data.get("requestId")
    generation = session.connection_generation
    try:
        params = SampleParams.from_wire(data)
        result = await session.command_dispatcher.thread_samples.sample(request_id, params, generation)
    except Exception as error:
        if not isinstance(error, SampleError):
            logger.exception("Thread sample failed")
        message = str(error) if isinstance(error, SampleError) else sanitize_llm_error_message(error)
        await emit_command_error(session, "thread/sample", message, data={"requestId": request_id,
            "conversation_id": data.get("threadId", ""),
            "error_code": error.code if isinstance(error, SampleError) else "sample_failed"})
    else:
        await session.emit_command_result("thread/sample", "", data={"requestId": request_id,
            "conversation_id": params.thread_id, **result})
    return True


async def handle_cancel(session, data):
    try:
        request_key(data.get("requestId"))
        session.command_dispatcher.thread_samples.cancel(data["requestId"], session.connection_generation)
    except SampleError as error:
        await emit_command_error(session, "thread/sample/cancel", error, data={"error_code": error.code})
    else:
        await session.emit_command_result("thread/sample/cancel", "", data={})
    return True


HANDLERS = {"thread/sample": handle_sample, "thread/sample/cancel": handle_cancel}
