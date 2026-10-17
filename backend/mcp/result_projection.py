"""Source CallToolResult model body; display/log text is a separate projection."""
from __future__ import annotations

from collections import OrderedDict
import hashlib
import json
import math
from threading import Lock
from typing import Any, cast

from backend.network.native import NativeNetworkRuntime


class _MediaEstimate:
    def __init__(self):
        self.initialization_lock = Lock()
        self.initialized = False
        self.value: int | None = None


# Source keeps 32 SHA-1 entries and initializes each entry outside the LRU lock.
_audio_estimates: OrderedDict[bytes, _MediaEstimate] = OrderedDict()
_image_estimates: OrderedDict[bytes, _MediaEstimate] = OrderedDict()
_audio_estimate_lock = Lock()
_image_estimate_lock = Lock()


def mcp_model_output(result: dict[str, Any]) -> str | list[dict[str, Any]]:
    items = []
    for block in result["content"]:
        kind = block.get("type") if isinstance(block, dict) else None
        raw_meta = block.get("_meta") if isinstance(block, dict) else None
        meta = raw_meta if isinstance(raw_meta, dict) else {}
        if kind == "text" and isinstance(block.get("text"), str):
            items.append({"type": "encrypted_content", "encrypted_content": block["text"]}
                if meta.get("codex/encryptedContent") is True else {"type": "input_text", "text": block["text"]})
        elif kind in {"image", "audio"} and isinstance(block.get("data"), str):
            data = block["data"]
            mime = block.get("mimeType", block.get("mime_type")) or "application/octet-stream"
            url = data if data.startswith("data:") else f"data:{mime};base64,{data}"
            if kind == "image":
                detail = meta.get("codex/imageDetail")
                items.append({"type": "input_image", "image_url": url,
                    "detail": detail if detail in {"auto", "low", "high", "original"} else "high"})
            else:
                items.append({"type": "input_audio", "audio_url": url})
        else:
            items.append({"type": "input_text", "text": json.dumps(block, ensure_ascii=False, separators=(",", ":"))})
    if not any(item["type"] == "encrypted_content" for item in items) and result.get("structuredContent") is not None:
        return json.dumps(result["structuredContent"], ensure_ascii=False, separators=(",", ":"))
    return items


def mcp_text_output(output: str | list[dict[str, Any]]) -> str:
    if isinstance(output, str):
        return output
    if any(item["type"] == "encrypted_content" for item in output):
        raise ValueError("MCP encrypted content requires the Codex-compatible Responses transport")
    return "\n".join(item["text"] for item in output if item["type"] == "input_text")


def mcp_log_output(result: dict[str, Any]) -> str:
    return mcp_output_log_text(mcp_model_output(result))


def mcp_output_log_text(output: str | list[dict[str, Any]]) -> str:
    return output if isinstance(output, str) else "\n".join(item["text"] for item in output
        if item["type"] == "input_text" and item["text"].strip())


def mcp_output_images(output: str | list[dict[str, Any]]) -> list[dict[str, str]]:
    if isinstance(output, str):
        return []
    images = []
    for item in output:
        if item["type"] == "input_image" and item["image_url"].startswith("data:"):
            metadata, data = item["image_url"].split(",", 1)
            images.append({"media_type": metadata[5:].split(";", 1)[0], "data": data})
    return images


def _approx_tokens(text: str) -> int:
    return (len(text.encode("utf-8")) + 3) // 4


def _truncate_text(text: str, mode: str, limit: int) -> str:
    """utils/string/truncate.rs retains ends; the marker is outside the budget."""
    encoded = text.encode("utf-8")
    budget = limit * 4 if mode == "tokens" else limit
    if len(encoded) <= budget:
        return text
    left = encoded[:budget // 2].decode("utf-8", errors="ignore")
    right_budget = budget - budget // 2
    right = encoded[-right_budget:].decode("utf-8", errors="ignore") if right_budget else ""
    omitted = ((len(encoded) - budget + 3) // 4 if mode == "tokens"
        else len(text) - len(left) - len(right))
    return f"{left}…{omitted} {'tokens' if mode == 'tokens' else 'chars'} truncated…{right}"


def estimate_audio_tokens(url: str) -> int:
    key = hashlib.sha1(url.encode("utf-8")).digest()
    with _audio_estimate_lock:
        entry = _audio_estimates.get(key)
        if entry is None:
            entry = _MediaEstimate()
            _audio_estimates[key] = entry
        _audio_estimates.move_to_end(key)
        if len(_audio_estimates) > 32:
            _audio_estimates.popitem(last=False)
    with entry.initialization_lock:
        if not entry.initialized:
            entry.value = NativeNetworkRuntime().invoke_sync("media/audioTokens", {"audioUrl": url})["tokens"]
            entry.initialized = True
        return cast(int, entry.value)


def estimate_original_image_bytes(url: str) -> int | None:
    key = hashlib.sha1(url.encode("utf-8")).digest()
    with _image_estimate_lock:
        entry = _image_estimates.get(key)
        if entry is None:
            entry = _MediaEstimate()
            _image_estimates[key] = entry
        _image_estimates.move_to_end(key)
        if len(_image_estimates) > 32:
            _image_estimates.popitem(last=False)
    with entry.initialization_lock:
        if not entry.initialized:
            entry.value = NativeNetworkRuntime().invoke_sync("media/originalImageBytes", {"imageUrl": url})["bytes"]
            entry.initialized = True
        return entry.value


def mcp_output_visible_bytes(output: str | list[dict[str, Any]]) -> int:
    if isinstance(output, str):
        return len(output.encode("utf-8"))
    total = 0
    for item in output:
        kind = item["type"]
        if kind == "input_text":
            total += len(item["text"].encode("utf-8"))
        elif kind == "input_audio":
            total += estimate_audio_tokens(item["audio_url"]) * 4
        elif kind == "encrypted_content":
            total += (len(item["encrypted_content"].encode("utf-8")) * 9 + 15) // 16
        elif kind == "input_image":
            size = estimate_original_image_bytes(item["image_url"]) if item.get("detail") == "original" else None
            total += 7373 if size is None else size
    return total


def budget_mcp_output(result: dict[str, Any], *, wall_time: float, policy: dict[str, Any] | None,
                      output_token_limit: int | None, original_image_detail_supported: bool) -> str | list[dict[str, Any]]:
    # models-manager/model_info.rs uses Bytes(10_000) for an unknown model.
    effective = dict(policy) if policy is not None else {"mode": "bytes", "limit": 10000}
    mode, limit = effective["mode"], effective["limit"]
    if output_token_limit is not None and output_token_limit * 4 < (limit * 4 if mode == "tokens" else limit):
        mode, limit = "tokens", output_token_limit
    limit = math.ceil(limit * 1.2)
    output = mcp_model_output(result)
    header = f"Wall time: {wall_time:.4f} seconds\nOutput:"
    if isinstance(output, str):
        return _truncate_text(header + ("\n" + output if output else ""), mode, limit)
    items = [{"type": "input_text", "text": header}, *output]
    remaining = limit
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
            cost = _approx_tokens(text) if mode == "tokens" else len(text.encode("utf-8"))
            projected.append({"type": "input_text", "text": text if cost <= remaining else _truncate_text(text, mode, remaining)})
            remaining = max(0, remaining - cost)
        elif kind == "input_audio":
            cost = estimate_audio_tokens(item["audio_url"]) * (4 if mode == "bytes" else 1)
            if cost <= remaining:
                projected.append(item)
                remaining -= cost
            else:
                omitted_audio += 1
        elif kind == "input_image":
            if item["detail"] == "original" and not original_image_detail_supported:
                item = {**item, "detail": "high"}
            projected.append(item)
        else:
            projected.append(item)
    if omitted_text:
        projected.append({"type": "input_text", "text": f"[omitted {omitted_text} text items ...]"})
    if omitted_audio:
        projected.append({"type": "input_text", "text": f"[omitted {omitted_audio} audio items ...]"})
    return projected
