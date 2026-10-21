"""Desktop voice control bound to the original, actually running call."""
from __future__ import annotations

import json

from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema
from backend.tools.contracts import ToolSpec

def active_realtime_service(context):
    if context is None or context.run_context is None or context.run_context.is_subagent:
        return None
    owner = context.run_context.environment_thread_runtime
    if (owner is None or owner.conversation_id != context.conversation_id
            or owner.store_identity != context.metadata.get("conversation_store_id")):
        return None
    service = owner.realtime_runtime
    if service is None or not service.active or not service.started or service.closing:
        return None
    return service


class EndRealtimeVoiceCallTool(BaseTool):
    name = "end_realtime_voice_call"
    description = "End the current voice chat. Only call this tool if the user explicitly asks to end the voice chat."
    permission = PermissionLevel.AUTO

    def get_spec(self):
        return ToolSpec(self.name, toolset="voice", capability="realtime.control", exposure="deferred_model_only")

    def get_schema(self):
        return ToolSchema(self.name, self.description,
            {"type": "object", "properties": {}, "required": [], "additionalProperties": False})

    async def execute(self, args, context=None):
        service = active_realtime_service(context)
        policy = context.metadata.get("_toolset_policy") if context is not None else None
        if service is None or policy is None or policy.realtime_service is not service:
            return ToolResult(content="There is no active voice chat owned by this task's current tool request.", is_error=True)
        await service.aclose()
        return ToolResult(content=json.dumps({"ended": True}))


async def _desktop_voice_request(name, args, context):
    service = active_realtime_service(context)
    policy = context.metadata.get("_toolset_policy") if context is not None else None
    if (service is None or policy is None or policy.realtime_service is not service
            or service.desktop_client is None or not service.desktop_client.supports(name)):
        return ToolResult(content="This tool is only available during this task's original active desktop voice chat.", is_error=True)
    try:
        value = await service.desktop_client.invoke(conversation_id=context.conversation_id,
            name=name, arguments=args, call_id=context.tool_call_id)
    except ValueError as error:
        return ToolResult(content=str(error), is_error=True)
    if name == "capture_screen_context" and active_realtime_service(context) is not service:
        return ToolResult(content="Screen context is unavailable after the original voice chat ends.", is_error=True)
    return ToolResult(content=json.dumps(value, ensure_ascii=False))


class CaptureScreenContextTool(BaseTool):
    name = "capture_screen_context"
    description = ("Only use this tool during an active voice chat for the current task. Never load or call it from a normal text conversation or after voice chat ends. "
        "Read the current MiniCode page and right sidebar state when MiniCode is foreground. Screen context from other apps is not supported on this device. Do not guess screen details.")
    permission = PermissionLevel.AUTO

    def get_spec(self):
        return ToolSpec(self.name, toolset="voice", capability="realtime.capture", exposure="deferred_model_only")

    def get_schema(self):
        return ToolSchema(self.name, self.description, {"type": "object", "properties": {}, "required": [], "additionalProperties": False})

    async def execute(self, args, context=None):
        return await _desktop_voice_request(self.name, args, context)


class TransferVoiceCallTool(BaseTool):
    name = "transfer_voice_call"
    description = "Transfer the active voice call to another MiniCode task, or return it to the task the user was previously speaking with. Use only when the user asks to speak to another task or return. Provide a concise handoff context when useful."
    permission = PermissionLevel.AUTO

    def get_spec(self):
        return ToolSpec(self.name, toolset="voice", capability="realtime.transfer", exposure="deferred_model_only")

    def get_schema(self):
        context = {"type": "string", "maxLength": 4000}
        return ToolSchema(self.name, self.description, {"type": "object", "oneOf": [
            {"type": "object", "properties": {"threadId": {"type": "string", "minLength": 1},
                "hostId": {"type": "string", "minLength": 1}, "context": context}, "required": ["threadId"], "additionalProperties": False},
            {"type": "object", "properties": {"return": {"const": True}, "context": context}, "required": ["return"], "additionalProperties": False},
        ]})

    async def execute(self, args, context=None):
        return await _desktop_voice_request(self.name, args, context)
