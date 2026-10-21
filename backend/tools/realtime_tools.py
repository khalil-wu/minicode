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
