"""One inherited generation, captured before the harness can execute its output."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import dataclass
from typing import Any

from backend.agent.query_engine import QueryEngine, QuerySubmission
from backend.llm.base import StreamEvent, StreamEventType, capture_response_items

SAMPLE_TIMEOUT = 30.0
MAX_PROMPT_BYTES = 4_000
MAX_OUTPUT_BYTES = 64 * 1024


class SampleError(RuntimeError):
    def __init__(self, message: str, *, invalid_request: bool = False):
        super().__init__(message)
        self.code = "invalid_request" if invalid_request else "sample_failed"


@dataclass(frozen=True)
class SampleParams:
    thread_id: str
    expected_turn_id: str
    prompt: str
    effort: str | None = None
    thread_source: str = "feature:thread_sample"
    turn_trigger: str | None = None
    responsesapi_client_metadata: dict[str, str] | None = None
    disable_mcp: bool = False

    @classmethod
    def from_wire(cls, data: dict[str, Any]) -> SampleParams:
        for name in ("threadId", "expectedTurnId", "prompt"):
            if not isinstance(data.get(name), str) or not data[name].strip():
                raise SampleError(f"thread/sample requires {name}", invalid_request=True)
        if len(data["prompt"].encode("utf-8")) > MAX_PROMPT_BYTES:
            raise SampleError(f"sample prompt exceeds {MAX_PROMPT_BYTES} bytes", invalid_request=True)
        effort = data.get("effort")
        if effort is not None and effort not in {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}:
            raise SampleError("invalid sample reasoning effort", invalid_request=True)
        source = data.get("threadSource")
        if source is None:
            source = "feature:thread_sample"
        if not isinstance(source, str) or not (source in {"user", "subagent", "guardian_review", "memory_consolidation"}
                or source.startswith("feature:") and source[8:].strip()):
            raise SampleError("invalid sample threadSource", invalid_request=True)
        trigger = data.get("turnTrigger")
        metadata = data.get("responsesapiClientMetadata")
        if trigger is not None and not isinstance(trigger, str):
            raise SampleError("turnTrigger must be a string", invalid_request=True)
        if metadata is not None and (not isinstance(metadata, dict)
                or any(not isinstance(key, str) or not isinstance(value, str) for key, value in metadata.items())):
            raise SampleError("responsesapiClientMetadata must contain strings", invalid_request=True)
        if type(data.get("disableMcp", False)) is not bool:
            raise SampleError("disableMcp must be a boolean", invalid_request=True)
        return cls(data["threadId"], data["expectedTurnId"], data["prompt"], effort, source, trigger,
            dict(metadata) if metadata is not None else None, data.get("disableMcp", False))


async def collect_sample(upstream: AsyncIterator[StreamEvent]) -> dict[str, Any]:
    parts: dict[str, str] = {}
    size = 0
    text_only = True
    usage = None

    def put(identity: str, text: str, *, replace: bool = False) -> None:
        nonlocal size
        previous = parts.get(identity, "")
        replacement = text if replace else previous + text
        size += len(replacement.encode("utf-8")) - len(previous.encode("utf-8"))
        if size > MAX_OUTPUT_BYTES:
            raise SampleError("sample output exceeds limit")
        parts[identity] = replacement

    async for event in upstream:
        if event.type == StreamEventType.ERROR:
            raise SampleError(f"sample failed: {event.content}")
        if event.usage is not None:
            usage = event.usage
        if event.type in {StreamEventType.TOOL_CALL_START, StreamEventType.TOOL_CALL_DELTA,
                StreamEventType.TOOL_CALL, StreamEventType.IMAGE_CHUNK, StreamEventType.PROVIDER_ACTIVITY}:
            text_only = False
        if event.type == StreamEventType.TEXT_CHUNK and event.phase != "commentary":
            if event.content_kind == "refusal":
                text_only = False
            else:
                put(event.item_id, event.content)
        for item in event.provider_items:
            kind = item["type"]
            if kind == "message" and item.get("phase") == "commentary":
                parts.pop(item.get("id", event.item_id), None)
                size = sum(len(text.encode("utf-8")) for text in parts.values())
            elif kind == "message" and item.get("role") == "assistant":
                content = item.get("content", [])
                text_only = text_only and all(part["type"] == "output_text" for part in content)
                put(item.get("id", event.item_id), "".join(part["text"] for part in content
                    if part["type"] == "output_text"), replace=True)
            elif kind == "anthropic_message":
                text_only = text_only and all(part["type"] in {"text", "thinking", "redacted_thinking"}
                    for part in item.get("content", []))
            elif kind not in {"reasoning", "chat_reasoning"}:
                text_only = False
        if event.type == StreamEventType.DONE:
            text = "".join(parts.values())
            return {"responseId": event.response_id, "text": text if text_only and text.strip() else None,
                "inputTokens": usage.input_tokens if usage is not None else None,
                "cachedInputTokens": usage.cache_read_input_tokens if usage is not None else None,
                "outputTokens": usage.output_tokens if usage is not None else None}
    raise SampleError("sample stream ended before completion")


class SampleCapture:
    def __init__(self):
        self.result: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()

    async def intercept(self, upstream: AsyncIterator[StreamEvent]) -> None:
        token = capture_response_items.set(True)
        try:
            try:
                result = await collect_sample(upstream)
            except Exception as error:
                self.result.set_exception(error)
            else:
                self.result.set_result(result)
            # Completion would allow Stop hooks and EOF would allow recovery.
            # The sample owner cancels and drains this exact provider reader.
            await asyncio.Future()
        finally:
            capture_response_items.reset(token)

    async def reject_interaction(self, *_args, **_kwargs):
        error = SampleError("sample turn requires client interaction")
        if not self.result.done():
            self.result.set_exception(error)
        raise error

    async def observe(self, event_type: str, data: dict[str, Any]) -> None:
        if event_type in {"control_request", "approval_request", "elicitation_dialog", "user_input.requested",
                "permission.requested", "plugin.install.requested"}:
            await self.reject_interaction()


async def sample_turn(submission: QuerySubmission, *, cancel: asyncio.Event, deadline: float) -> dict[str, Any]:
    capture = submission.runtime.run_context.response_capture
    errors = []

    async def consume():
        async with aclosing(QueryEngine().submit(submission)) as events:
            async for event in events:
                await capture.observe(event.type, event.data)
                if event.type == "error":
                    errors.append(event.data["message"])

    turn = asyncio.create_task(consume(), name="thread-sample-turn")
    stopped = asyncio.create_task(cancel.wait())
    try:
        completed, _ = await asyncio.wait({turn, stopped, capture.result}, timeout=max(0, deadline - asyncio.get_running_loop().time()),
            return_when=asyncio.FIRST_COMPLETED)
        if cancel.is_set():
            raise SampleError("sample canceled")
        if not completed or asyncio.get_running_loop().time() >= deadline:
            raise SampleError("sample timed out")
        if capture.result.done():
            return capture.result.result()
        await turn
        raise SampleError(errors[-1] if errors else "sample completed without a response")
    finally:
        submission.runtime.cancel_event.set()
        turn.cancel()
        stopped.cancel()
        results = await asyncio.gather(turn, stopped, return_exceptions=True)
        capture.result.cancel()
        if not capture.result.cancelled():
            capture.result.exception()
        if isinstance(results[0], Exception):
            raise results[0]
