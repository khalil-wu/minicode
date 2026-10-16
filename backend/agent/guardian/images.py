"""Source guardian-context/images.rs: ordered four-image/eight-MiB selection."""
from __future__ import annotations

from collections import deque
from copy import deepcopy
from dataclasses import dataclass

from backend.llm.native_compaction import native_compaction_windows

MAX_TRANSCRIPT_IMAGES = 4
MAX_TRANSCRIPT_IMAGE_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class TranscriptImages:
    images: tuple[dict, ...]
    omitted_bytes: int


def select_images(candidates) -> TranscriptImages:
    selected = deque()
    reference_bytes = 0
    omitted_bytes = 0
    for image in candidates:
        reference = image["image_url"] if "image_url" in image else image["file_id"]
        size = len(reference.encode("utf-8"))
        if size > MAX_TRANSCRIPT_IMAGE_BYTES:
            omitted_bytes += size
            continue
        while len(selected) >= MAX_TRANSCRIPT_IMAGES or reference_bytes + size > MAX_TRANSCRIPT_IMAGE_BYTES:
            previous, previous_bytes = selected.popleft()
            reference_bytes -= previous_bytes
            omitted_bytes += previous_bytes
        selected.append((deepcopy(image), size))
        reference_bytes += size
    return TranscriptImages(tuple(image for image, _ in selected), omitted_bytes)


def collect_parent_images(context, *, include_tool_outputs: bool, node_repl) -> TranscriptImages:
    builder = context.run_context.context_builder if context.run_context is not None else None
    candidates = []
    def content_images(content):
        if isinstance(content, list):
            candidates.extend(deepcopy(item) for item in content if item.get("type") == "input_image")
    def response_item(item):
        kind = item["type"]
        if kind == "message" and item["role"] in {"user", "assistant"}:
            content_images(item["content"])
        elif kind in {"function_call_output", "custom_tool_call_output"} and include_tool_outputs:
            content_images(item["output"])
    if builder is not None:
        for message in builder._history:
            windows = native_compaction_windows(message)
            if windows:
                for window in windows:
                    for item in window["output"]:
                        response_item(item)
                continue
            native_images = any(item["type"] in {"message", "function_call_output", "custom_tool_call_output"} for item in message.provider_items)
            if not native_images:
                output = next((body for body in (message.native_tool_output, message.mcp_output, message.code_mode_output, message.history_notes_output) if body is not None), None)
                if message.role == "tool" and include_tool_outputs and output is not None:
                    content_images(output)
                elif message.role in {"user", "assistant"} or message.role == "tool" and include_tool_outputs:
                    candidates.extend({"type": "input_image", "image_url": f"data:{part['media_type']};base64,{part['data']}",
                        **({"detail": part["detail"]} if part.get("detail") is not None else {})} for part in message.images)
            for item in message.provider_items:
                response_item(item)
    if include_tool_outputs and node_repl is not None:
        candidates.extend(node_repl.images())
    return select_images(candidates)
