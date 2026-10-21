"""Source thread/realtime commands, routed through their original thread owner."""
from __future__ import annotations

from pydantic import ValidationError

from backend.realtime.protocol import COMMAND_PARAMS, voices
from backend.realtime.session import start_runtime, thread_runtime
from backend.ws.command_results import emit_command_error


async def handle_realtime(session, data, *, operation):
    command = "thread/realtime/" + operation
    conversation_id = data.get("threadId", "")
    try:
        if operation == "listVoices" and not conversation_id:
            if any(key not in {"conversation_id", "client_command_id"} for key in data):
                raise ValueError("thread/realtime/listVoices accepts no parameters")
            await session.emit_command_result(command, "", data=voices())
            return True
        if conversation_id != data.get("conversation_id") or not conversation_id:
            raise ValueError("threadId must match the addressed conversation")
        if session.conversation_repo.get_conversation_summary(conversation_id) is None:
            raise ValueError("The realtime thread was not found")
        params_data = {key: value for key, value in data.items()
            if key not in {"conversation_id", "client_command_id", "owner_conversation_id", "ownerConversationId"}
                and not (operation == "start" and key == "desktopVoice")}
        if operation == "listVoices":
            from backend.realtime.protocol import Thread
            Thread.model_validate(params_data)
            result = voices()
        else:
            params = COMMAND_PARAMS[operation].model_validate(params_data)
            owner = thread_runtime(session, params.thread_id)
            if operation == "start":
                from backend.realtime.desktop_client import DesktopVoiceClaim
                claim = DesktopVoiceClaim.model_validate(data["desktopVoice"]) if "desktopVoice" in data else None
                await start_runtime(session, params, desktop_voice=claim)
            elif operation == "stop":
                if owner.realtime_runtime is not None:
                    await owner.realtime_runtime.aclose()
            else:
                service = owner.realtime_runtime
                if service is None:
                    raise ValueError("This thread has no realtime session")
                if operation == "appendAudio":
                    await service.append_audio(params.audio)
                elif operation == "appendText":
                    await service.append_text(params.text, params.role)
                else:
                    await service.append_speech(params.text)
            result = {}
        if operation != "appendAudio" or data.get("client_command_id"):
            await session.emit_command_result(command, "", data={"conversation_id": conversation_id, **result})
    except (ValueError, ValidationError) as exc:
        await emit_command_error(session, command, exc, data={"conversation_id": conversation_id})
    return True


from functools import partial
HANDLERS = {"thread/realtime/" + operation: partial(handle_realtime, operation=operation)
    for operation in (*COMMAND_PARAMS, "listVoices")}
