"""Source Code Mode content ordering, media normalization and result budgets."""
from __future__ import annotations

import base64
import binascii
import math
from copy import deepcopy
from typing import Any

from backend.mcp.result_projection import _truncate_text, estimate_audio_tokens


def wav_duration_seconds(audio_url: str) -> float | None:
    """Measure PCM/float WAV using the bytes present, including streaming headers."""
    metadata, separator, payload = audio_url.partition(",")
    if not separator or not any(part.lower() == "base64" for part in metadata.split(";")[1:]):
        return None
    if len(payload) > math.ceil(50 * 1024 * 1024 / 3) * 4:
        return None
    try:
        data = base64.b64decode(payload, validate=True)
    except binascii.Error:
        return None
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return None
    offset = 12
    format_info = None
    while offset + 8 <= len(data):
        kind = data[offset:offset + 4]
        size = int.from_bytes(data[offset + 4:offset + 8], "little")
        chunk = data[offset + 8:offset + 8 + size]
        if kind == b"fmt ":
            if len(chunk) < 14:
                return None
            encoding = int.from_bytes(chunk[:2], "little")
            if encoding == 0xfffe:
                if chunk[26:40] != bytes([0, 0, 0, 0, 0x10, 0, 0x80, 0, 0, 0xaa, 0, 0x38, 0x9b, 0x71]):
                    return None
                encoding = int.from_bytes(chunk[24:26], "little")
            sample_rate = int.from_bytes(chunk[4:8], "little")
            block_align = int.from_bytes(chunk[12:14], "little")
            if encoding not in {1, 3} or not sample_rate or not block_align:
                return None
            format_info = sample_rate, block_align
        elif kind == b"data":
            if format_info is None:
                return None
            sample_rate, block_align = format_info
            return (len(chunk) // block_align) / sample_rate
        offset += 8 + size + size % 2
    return None


def normalize_runtime_item(item: dict[str, Any]) -> dict[str, Any]:
    if item["kind"] == "audio":
        duration = wav_duration_seconds(item["audio_url"])
        if duration is not None and duration < 0.025:
            return {"kind": "text", "text": "Audio output omitted because the clip is shorter than 25 ms; use a longer clip."}
    return item


def native_content_items(output: list[dict[str, Any]], error: str = "") -> list[dict[str, Any]]:
    items = []
    for item in output:
        kind = item["kind"]
        if kind == "text":
            items.append({"type": "input_text", "text": item["text"]})
        elif kind == "image":
            items.append({"type": "input_image", "image_url": item["image_url"], "detail": item["detail"]})
        elif kind == "audio":
            items.append({"type": "input_audio", "audio_url": item["audio_url"]})
    if error:
        items.append({"type": "input_text", "text": f"Script error:\n{error}"})
    return items


def code_mode_model_output(items: list[dict[str, Any]], *, status: str, wall_time: float,
                           max_tokens: int, original_supported: bool) -> list[dict[str, Any]]:
    """core/tools/code_mode/mod.rs budgets content before adding its host header."""
    items = deepcopy(items)
    for item in items:
        if item["type"] == "input_image" and item["detail"] == "original" and not original_supported:
            item["detail"] = "high"
    if all(item["type"] == "input_text" for item in items):
        combined = ""
        for item in items:
            if combined:
                combined += "\n"
            combined += item["text"]
        size = len(combined.encode("utf-8"))
        if size > max_tokens * 4:
            original_tokens = (size + 3) // 4
            lines = len(combined.splitlines())
            items = [{"type": "input_text", "text":
                f"Warning: truncated output (original token count: {original_tokens})\nTotal output lines: {lines}\n\n"
                + _truncate_text(combined, "tokens", max_tokens)}]
    else:
        remaining = max_tokens
        projected = []
        omitted_text = omitted_audio = 0
        for item in items:
            kind = item["type"]
            if kind == "input_text":
                text = item["text"]
                if not text:
                    continue
                if remaining == 0:
                    omitted_text += 1
                    continue
                cost = (len(text.encode("utf-8")) + 3) // 4
                projected.append({"type": "input_text", "text": text if cost <= remaining else _truncate_text(text, "tokens", remaining)})
                remaining = max(0, remaining - cost)
            elif kind == "input_audio":
                cost = estimate_audio_tokens(item["audio_url"])
                if cost <= remaining:
                    projected.append(item)
                    remaining -= cost
                else:
                    omitted_audio += 1
            else:
                projected.append(item)
        if omitted_text:
            projected.append({"type": "input_text", "text": f"[omitted {omitted_text} text items ...]"})
        if omitted_audio:
            projected.append({"type": "input_text", "text": f"[omitted {omitted_audio} audio items ...]"})
        items = projected
    rounded_time = math.floor(wall_time * 10 + 0.5) / 10
    return [{"type": "input_text", "text": f"{status}\nWall time {rounded_time:.1f} seconds\nOutput:\n"}, *items]


def _decode_data_url(url: str) -> tuple[str, bytes] | None:
    if not url.startswith("data:") or "," not in url:
        return None
    metadata, payload = url[5:].split(",", 1)
    parts = metadata.split(";")
    if not any(part.lower() == "base64" for part in parts[1:]):
        return None
    try:
        return parts[0].lower(), base64.b64decode(payload, validate=True)
    except binascii.Error:
        return None


def selected_media(output: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    """Playable artifacts use actual data bytes; the native body retains the URL."""
    media = []
    for item in output:
        if item["kind"] != kind:
            continue
        url = item[f"{kind}_url"]
        decoded = _decode_data_url(url)
        if decoded is None:
            continue
        media.append({"media_type": decoded[0], "data": url.split(",", 1)[1],
            **({"detail": item["detail"]} if kind == "image" else {}),
            **{key: item[key] for key in ("artifact_id", "source", "tool_call_id", "call_source", "operation") if key in item}})
    return media
