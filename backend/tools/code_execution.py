"""Model-facing execute/wait tools for the turn-owned JavaScript runtime."""
from __future__ import annotations

import base64
import json
from dataclasses import replace

from backend.agent.message import AgentEvent
from backend.async_cleanup import to_thread_cancel_safe
from backend.permissions.context import ToolExecutionContext
from backend.tools.base import BaseTool, ToolResult, ToolSchema, artifact_owner_workspace_root, truncate_tool_result, validate_tool_input


async def _present_result(result: ToolResult, context: ToolExecutionContext, max_chars: int) -> ToolResult:
    if len(result.content) > max_chars:
        artifact_id = await to_thread_cancel_safe(context.artifact_store.save, result.content, source="tool_exec.output",
            conversation_id=context.conversation_id, workspace_root=artifact_owner_workspace_root(context))
        report = result.runtime_metadata["code_cell"]
        preview = {"cell_id": report["cell_id"], "status": report["status"], "artifact_id": artifact_id,
                   "output_preview": truncate_tool_result(result.content, max_chars)}
        encoded = json.dumps(preview, ensure_ascii=False)
        excess = max(0, len(encoded) - max_chars)
        if excess:
            preview["output_preview"] = preview["output_preview"][:max(0, len(preview["output_preview"]) - excess)]
            encoded = json.dumps(preview, ensure_ascii=False)
        result = replace(result, content=encoded, artifact_id=artifact_id)
    for image in result.images:
        image_bytes = len(base64.b64decode(image["data"], validate=True))
        artifact_id = await to_thread_cancel_safe(context.artifact_store.save, image["data"], source="tool_exec.image", type="image",
            media_type=image["media_type"], conversation_id=context.conversation_id, workspace_root=artifact_owner_workspace_root(context))
        await context.run_context.publish_nested_event(AgentEvent("artifact.preview", {
            "artifact_id": artifact_id, "conversation_id": context.conversation_id,
            "message_id": str(context.metadata.get("assistant_message_id") or ""),
            "kind": "image", "media_type": image["media_type"], "summary": "Code cell image",
            "bytes": image_bytes,
        }))
    return result


class ToolExecTool(BaseTool):
    name = "tool_exec"
    read_only = True
    orchestrates_tools = True
    idempotent = False
    max_result_chars = None
    description = (
        "Run JavaScript to compose tools and filter results before showing them to the model. "
        "Use await tools.name(args), Promise.all/allSettled for independent calls, and text(value), image(image_block), or audio(audio_block) for selected output. "
        "audio accepts an audio data URL, {data, media_type}, or an MCP audio block. Audio is saved for user playback; it is not transcribed or heard by the model. "
        "Tool results expose content, status, is_error, images, audios and MCP structured_content. ALL_TOOLS lists names, descriptions and parameter schemas. "
        "Every nested call still requires the normal tool permission and budget. No filesystem, network, process or imports are available in JavaScript. "
        "store/load retain JSON values for later cells in this session; cells have fresh globals. "
        "Use tool_wait when status is running. Await every tool promise; unawaited calls are discarded. "
        "Supports setTimeout, clearTimeout, notify, yield_control and exit. Limits: 10 seconds of JavaScript execution, 128 MiB heap, 8 MiB stored values. "
        "An optional first line // @exec: {\"yield_time_ms\":1000,\"max_chars\":8000} sets polling/output options for raw-code calls."
    )

    def is_concurrency_safe(self, args=None):
        return False

    def get_schema(self) -> ToolSchema:
        return ToolSchema(self.name, self.description, {
            "type": "object", "additionalProperties": False,
            "properties": {"code": {"type": "string", "minLength": 1, "maxLength": 128000,
                                      "description": "JavaScript with awaited tools.name(args) calls; use text/image/audio to return output."},
                "yield_time_ms": {"type": "integer", "minimum": 0, "maximum": 10000,
                                  "description": "Milliseconds to wait before yielding a running cell_id; default 1000."},
                "max_chars": {"type": "integer", "minimum": 256, "maximum": 50000,
                              "description": "Maximum characters of returned text; default 8000."}},
            "required": ["code"],
        }, freeform={"input_field": "code", "description": self.description, "format": {"type": "text"}})

    async def execute(self, args, context: ToolExecutionContext | None = None) -> ToolResult:
        if context is None or context.run_context is None or context.run_context.code_execution is None:
            return ToolResult("tool_exec requires a QueryEngine-owned code runtime.", is_error=True, status="failed")
        first = args["code"].splitlines()[0]
        if first.startswith("// @exec:"):
            options = json.loads(first[len("// @exec:"):])
            args = {**args, **options, "code": args["code"]}
            error = validate_tool_input(self, args)
            if error: return ToolResult(error, is_error=True, status="failed")
        result = await context.run_context.code_execution.execute(args["code"], context, yield_time_ms=args.get("yield_time_ms", 1000))
        return await _present_result(result, context, args.get("max_chars", 8000))


class ToolWaitTool(BaseTool):
    name = "tool_wait"
    read_only = True
    orchestrates_tools = True
    idempotent = False
    max_result_chars = None
    description = "Read new output or completion from a tool_exec cell. Use the exact cell_id returned by tool_exec. terminate=true stops that cell and drains nested tools. If a cell is unavailable after restart, inspect recorded outcomes and workspace state before retrying writes."

    def is_concurrency_safe(self, args=None):
        return False

    def get_schema(self) -> ToolSchema:
        return ToolSchema(self.name, self.description, {"type": "object", "additionalProperties": False,
            "properties": {"cell_id": {"type": "string", "description": "Exact running cell_id returned by tool_exec or tool_wait."},
                "yield_time_ms": {"type": "integer", "minimum": 0, "maximum": 10000,
                                  "description": "Milliseconds to wait for new output; default 1000."},
                "terminate": {"type": "boolean", "description": "Stop this cell and its pending tool calls; default false."},
                "max_chars": {"type": "integer", "minimum": 256, "maximum": 50000,
                              "description": "Maximum characters of returned text; default 8000."}},
            "required": ["cell_id"]})

    async def execute(self, args, context: ToolExecutionContext | None = None) -> ToolResult:
        if context is None or context.run_context is None or context.run_context.code_execution is None:
            return ToolResult("tool_wait requires a QueryEngine-owned code runtime.", is_error=True, status="failed")
        result = await context.run_context.code_execution.wait(args["cell_id"], yield_time_ms=args.get("yield_time_ms", 1000), terminate=args.get("terminate", False))
        return await _present_result(result, context, args.get("max_chars", 8000))
