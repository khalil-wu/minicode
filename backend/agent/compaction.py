"""Helpers for formatting and parsing LLM-based context compaction."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace

from backend.llm.base import LLMMessage, estimate_text_tokens
from backend.memory.text_utils import truncate_middle_tokens
from backend.llm.native_compaction import native_compaction_windows


@dataclass(frozen=True)
class CompactionOutput:
    summary: str


COMPACTION_USER_INPUT_MAX_TOKENS = 20_000


def retain_user_inputs(messages: list[LLMMessage], max_tokens: int) -> list[LLMMessage]:
    """Retain recent admitted user text independently of a generated summary.

    Media ownership is retained separately by ContextBuilder; these copies
    contain text and source timestamps, never duplicate native attachments.
    The caller has already removed trusted runtime wrappers.
    """
    remaining = max_tokens
    retained: list[LLMMessage] = []
    for message in reversed(messages):
        if remaining <= 0:
            break
        if not message.is_user_input or not message.content:
            continue
        content = truncate_middle_tokens(message.content, remaining)
        retained.append(replace(message, content=content, images=[], documents=[], attachment_refs=[], runtime_context=""))
        if estimate_text_tokens(message.content) > remaining:
            break
        remaining -= estimate_text_tokens(content)
    retained.reverse()
    return retained


def format_compaction_history(messages: list[LLMMessage]) -> str:
    """Serialize selected history into MiniCode's compaction transcript format."""
    if any(native_compaction_windows(message) for message in messages):
        raise ValueError("An encrypted compaction window cannot be rewritten as a text summary")
    tool_budget = 500
    tool_names = {
        call.id: call.name
        for message in messages
        for call in message.tool_calls or []
    }
    parts: list[str] = []
    for message in messages:
        if message.role == "user":
            content = str(message.content or "")
            if content:
                parts.append(f"[User]: {content}")
        elif message.role == "assistant":
            content = str(message.content or "")
            if content:
                parts.append(f"[Assistant]: {content}")
            if message.tool_calls:
                calls: list[str] = []
                for call in message.tool_calls:
                    args = ", ".join(
                        f"{key}={json.dumps(value, ensure_ascii=False, separators=(',', ':'))}"
                        for key, value in call.arguments.items()
                    )
                    calls.append(f"{call.name}({truncate_middle_tokens(args, tool_budget)}) [call_id={json.dumps(call.id)}]")
                parts.append(f"[Assistant tool calls]: {'; '.join(calls)}")
        elif message.role == "tool":
            content = str(message.content or "")
            identity = json.dumps({
                "call_id": message.tool_call_id,
                "name": message.name or tool_names.get(message.tool_call_id),
                "is_error": message.is_error,
            }, ensure_ascii=False, separators=(",", ":"))
            parts.append(f"[Tool result] {identity}: {truncate_middle_tokens(content, tool_budget)}")
        if message.attachment_refs:
            parts.append("[Attachments; originals available through read_artifact]: " + json.dumps(
                message.attachment_refs, ensure_ascii=False, separators=(",", ":"),
            ))
    return "\n\n".join(parts)


def parse_compaction_output(
    output: str,
) -> CompactionOutput:
    summary = str(output or "").strip()
    if not summary:
        raise ValueError("Compaction output is empty")
    return CompactionOutput(summary=summary)
