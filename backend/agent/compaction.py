"""Helpers for formatting and parsing LLM-based context compaction."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from backend.llm.base import LLMMessage


@dataclass(frozen=True)
class CompactionOutput:
    summary: str
    memdir_facts: list[str] = field(default_factory=list)


def format_compaction_history(messages: list[LLMMessage]) -> str:
    """Format conversation history for LLM-based compaction.

    The summarizer should see essentially the full conversation (cc feeds the
    whole history). User/assistant turns are passed verbatim; only raw tool
    output gets a generous safety cap to avoid a pathological blow-up — tool
    results are already micro-compacted/persisted before reaching history.
    """
    tool_cap = 8_000
    parts: list[str] = []
    for message in messages:
        if message.role == "user":
            parts.append(f"User: {message.content or ''}")
        elif message.role == "assistant" and message.content:
            parts.append(f"Assistant: {message.content}")
        elif message.role == "tool":
            content = message.content or ""
            if len(content) > tool_cap:
                content = f"{content[:tool_cap]}... [truncated from {len(content)} chars]"
            parts.append(f"Tool({message.name}): {content}")
    return "\n".join(parts)


def parse_compaction_output(
    output: str,
    *,
    parse_memory_directives: bool,
) -> CompactionOutput:
    if not parse_memory_directives or ("<summary>" not in output and "<memdir>" not in output):
        return CompactionOutput(summary=output)

    summary_match = re.search(r"<summary>(.*?)</summary>", output, re.DOTALL)
    memdir_match = re.search(r"<memdir>(.*?)</memdir>", output, re.DOTALL)

    summary = summary_match.group(1).strip() if summary_match else output
    memdir_text = memdir_match.group(1).strip() if memdir_match else ""
    return CompactionOutput(
        summary=summary,
        memdir_facts=_parse_memdir_facts(memdir_text) if memdir_text else [],
    )


def _parse_memdir_facts(memdir_text: str) -> list[str]:
    return [
        fact
        for fact in (
            line.strip("- *")
            for line in memdir_text.split("\n")
            if line.strip("- *")
        )
        if fact
    ]
