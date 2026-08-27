"""Helpers for formatting and parsing LLM-based context compaction."""

from __future__ import annotations

import json
from dataclasses import dataclass

from backend.llm.base import LLMMessage


@dataclass(frozen=True)
class CompactionOutput:
    summary: str


def format_compaction_history(messages: list[LLMMessage]) -> str:
    """Serialize selected history into MiniCode's compaction transcript format."""
    tool_cap = 2_000
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
                    calls.append(f"{call.name}({args})")
                parts.append(f"[Assistant tool calls]: {'; '.join(calls)}")
        elif message.role == "tool":
            content = str(message.content or "")
            if len(content) > tool_cap:
                truncated_chars = len(content) - tool_cap
                content = (
                    f"{content[:tool_cap]}\n\n"
                    f"[... {truncated_chars} more characters truncated]"
                )
            if content:
                parts.append(f"[Tool result]: {content}")
    return "\n\n".join(parts)


def parse_compaction_output(
    output: str,
) -> CompactionOutput:
    summary = str(output or "").strip()
    if not summary:
        raise ValueError("Compaction output is empty")
    return CompactionOutput(summary=summary)
