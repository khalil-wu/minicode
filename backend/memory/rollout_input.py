"""Source V2 evidence priorities over MiniCode's durable transcript items."""
from __future__ import annotations

import json
from collections.abc import Iterable

from backend.memory.text_utils import truncate_middle_bytes
from backend.secret_redaction import redact_secrets


_OMITTED = "[... response items omitted ...]\n"
_LABELS = ("human user", "assistant final", "other agent", "assistant commentary", "harness context", "tool")
_MAX_ROLLOUT_BYTES = 8 * 1024 * 1024
_MAX_RECORD_BYTES = 1024 * 1024


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _excluded_context(text: str) -> bool:
    text = text.strip().lower()
    return ((text.startswith("# agents.md instructions") and text.endswith("</instructions>"))
        or (text.startswith("<skill>") and text.endswith("</skill>")))


def _message(role: str, content: str, *, phase: str = "", content_kind: str = "", input_origin: str = "") -> dict:
    return {"type": "message", "role": role, "content": [{"type": "input_text", "text": content}],
        "phase": phase, "content_kind": content_kind, "input_origin": input_origin}


def _tool_items(record: dict) -> Iterable[dict]:
    identity = record.get("id") or record.get("tool_call_id") or ""
    name = record.get("name") or record.get("tool_name") or ""
    arguments = record.get("args", record.get("arguments", record.get("input", {})))
    yield {"type": "function_call", "name": name, "call_id": identity, "arguments": arguments}
    # A running/unstarted call is evidence of a request, never a completed result.
    if record.get("status") in {"pending", "running"}:
        return
    output = record.get("output", record.get("result", record.get("summary")))
    if output is not None:
        yield {"type": "function_call_output", "call_id": identity,
            "output": output if isinstance(output, str) else _json(output)}


def _message_items(message: dict) -> Iterable[dict]:
    role = message.get("role")
    if role not in {"user", "assistant", "tool"}:
        return
    if role == "tool":
        yield {"type": "function_call_output", "call_id": message.get("tool_call_id"), "output": message.get("content", "")}
        return
    blocks = message.get("blocks", ())
    if role == "assistant" and blocks:
        for block in blocks:
            kind = block.get("type")
            if kind == "tool_call":
                yield from _tool_items(block["record"])
            elif kind == "text" or kind == "process" and block.get("source") in {"model_commentary", "model_preamble", "post_tool"}:
                source = block.get("source", "")
                if source in {"model_final", "model_commentary", "model_preamble", "post_tool", "async", "retained", ""} and block.get("content"):
                    yield _message("assistant", block["content"], phase="commentary"
                        if source in {"model_commentary", "model_preamble", "post_tool"} else "final_answer")
            elif kind == "progress" and block.get("subagentId") and block.get("detail"):
                yield _message("user", _json({key: block[key] for key in ("subagentId", "subagentName", "subagentStatus", "detail") if key in block}),
                    content_kind="multi_agent.notification", input_origin="agent")
        return
    content = str(message.get("content") or "")
    if role == "user" and _excluded_context(content):
        return
    metadata = message.get("metadata") or {}
    item = _message(role, content, phase=message.get("phase", ""),
        content_kind=message.get("content_kind", metadata.get("content_kind", "")),
        input_origin=message.get("input_origin", metadata.get("input_origin", "")))
    for attachment in message.get("attachments", ()):
        media_type = str(attachment.get("media_type") or "")
        placeholder = "[image omitted]" if media_type.startswith("image/") else "[audio omitted]" if media_type.startswith("audio/") else "[file omitted]"
        item["content"].append({"type": "input_text", "text": placeholder})
    if content or len(item["content"]) > 1:
        yield item
    for record in message.get("tool_calls", ()):
        yield from _tool_items(record)


def _other_agent(item: dict) -> bool:
    if item.get("input_origin") == "agent" or item.get("content_kind", "").startswith("multi_agent."):
        return True
    for part in item.get("content", ()):
        text = part["text"].lstrip()
        lines = text.splitlines()
        if text.startswith("<subagent_notification>") or (len(lines) >= 4 and lines[0].startswith("Message Type:")
                and lines[1].startswith("Task name:") and lines[2].startswith("Sender:") and lines[3] == "Payload:"):
            return True
    return False


def serialize_tiered_input(transcript: list[dict], *, max_bytes: int) -> str:
    recent = []
    remaining = _MAX_ROLLOUT_BYTES
    for message in reversed(transcript):
        for item in reversed(list(_message_items(message))):
            size = len(_json(item).encode("utf-8"))
            if size > _MAX_RECORD_BYTES:
                continue
            if size > remaining:
                remaining = 0
                break
            recent.append(item)
            remaining -= size
        if not remaining:
            break
    rows = []
    questions = {}
    for item in reversed(recent):
        kind = item["type"]
        question = None
        if kind == "function_call" and item["name"] == "request_user_input":
            questions[item["call_id"]] = item["arguments"]
        elif kind == "function_call_output" and item["call_id"] in questions:
            try:
                reply = json.loads(item["output"])
            except (json.JSONDecodeError, TypeError):
                reply = None
            if isinstance(reply, dict) and isinstance(reply.get("answers"), dict) and any(
                isinstance(answer, dict) and any(isinstance(text, str) and text.strip() for text in answer.get("answers", ()))
                for answer in reply["answers"].values()
            ):
                question = questions.pop(item["call_id"])
        tier = 5
        if question is not None:
            tier = 0
        elif kind == "message":
            if _other_agent(item):
                tier = 2
            elif item["role"] == "user":
                contextual = (bool(item.get("content_kind")) and not item["content_kind"].startswith("user.")) or any(
                    part["text"].strip().startswith("<environment_context>") and part["text"].strip().endswith("</environment_context>")
                    for part in item["content"])
                tier = 4 if contextual else 0
            else:
                tier = 3 if item["phase"] == "commentary" else 1
        text = _json(item)
        if question is not None:
            text = f"Assistant question: {_json(question)}\nHuman reply: {text}"
        text = redact_secrets(text)
        if tier == 5 and kind == "function_call_output":
            text = truncate_middle_bytes(text, 8_000 - 96)
        rows.append((tier, f"[{_LABELS[tier]}]\n{truncate_middle_bytes(text, 10_000 - 96)}\n"))
    remaining = max(0, max_bytes - len(_OMITTED))
    selected = [None] * len(rows)
    for tier in range(len(_LABELS)):
        for index in range(len(rows) - 1, -1, -1):
            row_tier, text = rows[index]
            if row_tier != tier or remaining <= len(_OMITTED) + 96:
                continue
            budget = remaining - len(_OMITTED)
            if len(text.encode("utf-8")) > budget:
                text = truncate_middle_bytes(text, budget - 96)
            remaining = max(0, remaining - len(text.encode("utf-8")) - len(_OMITTED))
            selected[index] = text
    rendered = []
    gap = False
    for text in selected:
        if text is not None:
            rendered.append(text)
            gap = False
        elif not gap:
            rendered.append(_OMITTED)
            gap = True
    result = "".join(rendered)
    return result if len(result.encode("utf-8")) <= max_bytes else ""
