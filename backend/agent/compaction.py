"""Helpers for formatting and parsing LLM-based context compaction."""

from __future__ import annotations

from dataclasses import dataclass

from backend.llm.base import LLMMessage


@dataclass(frozen=True)
class CompactionOutput:
    summary: str


def format_compaction_history(messages: list[LLMMessage]) -> str:
    """Format conversation history for LLM-based compaction.

    Pi serializes selected conversation entries and caps tool output at 2,000
    characters before asking the model for a summary.
    """
    tool_cap = 2_000
    parts: list[str] = []
    for message in messages:
        if message.role == "user":
            parts.append(f"User: {message.content or ''}")
        elif message.role == "assistant":
            if message.content:
                parts.append(f"Assistant: {message.content}")
            if message.tool_calls:
                calls = [
                    {
                        "name": call.name,
                        "id": call.id,
                        "arguments": call.arguments,
                    }
                    for call in message.tool_calls
                ]
                parts.append(f"[Assistant tool calls] {calls}")
        elif message.role == "tool":
            content = message.content or ""
            if len(content) > tool_cap:
                content = f"{content[:tool_cap]}... [truncated from {len(content)} chars]"
            parts.append(f"Tool({message.name}): {content}")
    return "\n".join(parts)


def parse_compaction_output(output: str) -> CompactionOutput:
    return CompactionOutput(summary=str(output or "").strip())
