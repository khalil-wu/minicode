"""Model-facing execute/wait tools for the turn-owned JavaScript runtime."""
from __future__ import annotations

import base64
import json
from dataclasses import asdict, replace

from backend.agent.message import AgentEvent
from backend.async_cleanup import to_thread_cancel_safe
from backend.permissions.context import ToolExecutionContext
from backend.tools.base import BaseTool, ToolResult, ToolSchema, artifact_owner_workspace_root, validate_tool_input


async def _present_result(result: ToolResult, context: ToolExecutionContext, max_chars: int, operation: str) -> ToolResult:
    if len(result.content) > max_chars:
        artifact_id = await to_thread_cancel_safe(context.artifact_store.save, result.content, source="tool_exec.output",
            conversation_id=context.conversation_id, workspace_root=artifact_owner_workspace_root(context))
        report = result.runtime_metadata["code_cell"]
        # The wire report is one JSON line even when text() emitted source code.
        # Truncate the selected output, not that envelope: line truncation of the
        # envelope otherwise discards every useful byte of a long tool result.
        output = "\n".join([*([report["error"]] if report.get("error") else []), *report.get("output", [])])
        preview = {"cell_id": report["cell_id"], "status": report["status"], "artifact_id": artifact_id,
                   "output_preview": ""}
        marker = "\n... [truncated; full output in artifact] ...\n"
        preview["output_preview"] = marker
        encoded = json.dumps(preview, ensure_ascii=False)
        # JSON escaping also consumes the caller's budget (quotes, newlines,
        # control characters). Fit both ends without cutting the wire envelope.
        low, high = 0, min(len(output), max_chars)
        while low < high:
            keep = (low + high + 1) // 2
            head, tail = keep - keep // 3, keep // 3
            preview["output_preview"] = output[:head] + marker + (output[-tail:] if tail else "")
            candidate = json.dumps(preview, ensure_ascii=False)
            if len(candidate) <= max_chars:
                low, encoded = keep, candidate
            else:
                high = keep - 1
        result = replace(result, content=encoded, artifact_id=artifact_id)
    presented_images = []
    for image in result.images:
        image_bytes = len(base64.b64decode(image["data"], validate=True))
        owner = {"conversation_id": context.conversation_id, "workspace_root": artifact_owner_workspace_root(context)}
        artifact_id = image.get("artifact_id")
        if artifact_id:
            meta = await to_thread_cancel_safe(context.artifact_store.get_meta, artifact_id, **owner)
            original = await to_thread_cancel_safe(context.artifact_store.get, artifact_id, **owner)
            if meta is None or meta.type != "image" or meta.media_type != image["media_type"] or original != image["data"]:
                artifact_id = None
        if not artifact_id:
            artifact_id = await to_thread_cancel_safe(context.artifact_store.save, image["data"], source="tool_exec.image", type="image",
                media_type=image["media_type"], **owner)
        presented_images.append({**image, "artifact_id": artifact_id})
        await context.run_context.publish_nested_event(AgentEvent("artifact.preview", {
            "artifact_id": artifact_id, "conversation_id": context.conversation_id,
            "message_id": str(context.metadata.get("assistant_message_id") or ""),
            "kind": "image", "media_type": image["media_type"],
            "summary": "生成图片" if image.get("source") == "image_generation" else "代码执行图片输出",
            "bytes": image_bytes,
            "source": image.get("source", "tool"),
            **({"tool_call_id": image.get("tool_call_id") or context.tool_call_id} if image.get("tool_call_id") or context.tool_call_id else {}),
            "operation": image.get("operation") or operation,
            **({"call_source": image["call_source"]} if image.get("call_source") else
               {"call_source": asdict(context.source_for_call(context.tool_call_id))} if context.source_for_call(context.tool_call_id).kind != "direct" else {}),
        }))
    return replace(result, images=presented_images) if presented_images else result


class ToolExecTool(BaseTool):
    name = "tool_exec"
    read_only = True
    orchestrates_tools = True
    idempotent = False
    max_result_chars = None
    description = (
        "Run JavaScript to compose tools and filter results before showing them to the model. "
        "Use await tools.name(args), Promise.all/allSettled for independent calls, and text(value), image(image_block), or audio(audio_block) for selected output. "
        "image() publishes selected tool image evidence; generatedImage({image_url, output_hint?}) publishes an image-generation result. "
        "audio accepts an audio data URL, {data, media_type}, or an MCP audio block. Audio is saved for user playback; it is not transcribed or heard by the model. "
        "Tool results expose content, status, is_error, images, audios and MCP structured_content. ALL_TOOLS lists names, descriptions and parameter schemas. "
        "Every nested call still requires the normal tool permission and budget. No filesystem, network, process or imports are available in JavaScript. "
        "store/load retain JSON values for later cells in this session; cells have fresh globals. "
        "Use tool_wait when status is running; pending_tools names the unfinished operations. A yielded cell is still executing, not unavailable. "
        "For independent long tasks, emit each result as it finishes: await Promise.all(jobs.map(async job => text(await job))). "
        "Await every tool promise; unawaited calls are discarded. "
        "Supports setTimeout, clearTimeout, notify, yield_control and exit. Limits: 10 seconds of JavaScript execution, 128 MiB heap, 8 MiB stored values. "
        "An optional first line // @exec: {\"yield_time_ms\":10000,\"max_chars\":8000} sets polling/output options for raw-code calls."
    )

    def is_concurrency_safe(self, args=None):
        return False

    def get_schema(self) -> ToolSchema:
        return ToolSchema(self.name, self.description, {
            "type": "object", "additionalProperties": False,
            "properties": {"code": {"type": "string", "minLength": 1, "maxLength": 128000,
                                      "description": "JavaScript with awaited tools.name(args) calls; use text/image/audio to return output."},
                "yield_time_ms": {"type": "integer", "minimum": 0, "maximum": 10000,
                                  "description": "Milliseconds to wait before yielding a running cell_id; default 10000."},
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
            if error:
                return ToolResult(error, is_error=True, status="failed")
        result = await context.run_context.code_execution.execute(args["code"], context, yield_time_ms=args.get("yield_time_ms", 10000))
        return await _present_result(result, context, args.get("max_chars", 8000), self.name)


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
                                  "description": "Milliseconds to wait for new output; default 10000."},
                "terminate": {"type": "boolean", "description": "Stop this cell and its pending tool calls; default false."},
                "max_chars": {"type": "integer", "minimum": 256, "maximum": 50000,
                              "description": "Maximum characters of returned text; default 8000."}},
            "required": ["cell_id"]})

    async def execute(self, args, context: ToolExecutionContext | None = None) -> ToolResult:
        if context is None or context.run_context is None or context.run_context.code_execution is None:
            return ToolResult("tool_wait requires a QueryEngine-owned code runtime.", is_error=True, status="failed")
        result = await context.run_context.code_execution.wait(args["cell_id"], yield_time_ms=args.get("yield_time_ms", 10000), terminate=args.get("terminate", False))
        return await _present_result(result, context, args.get("max_chars", 8000), self.name)
