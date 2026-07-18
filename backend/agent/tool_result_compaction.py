from __future__ import annotations

import hashlib
from dataclasses import dataclass
from html import escape
from typing import Callable

from backend.llm.base import LLMMessage


TOOL_RESULT_CACHE_ENTRY_PREFIX = "<tool_result_cache_entry"
TOOL_RESULT_CACHE_ENTRY_SUFFIX = "</tool_result_cache_entry>"


@dataclass(frozen=True)
class ToolResultCacheEditConfig:
    keep_recent: int = 4
    min_chars: int = 600
    preview_chars: int = 260
    preserve_unconsumed_tool_batch: bool = True


@dataclass(frozen=True)
class ToolResultCacheEditStats:
    compacted: int = 0
    before_tokens: int = 0
    after_tokens: int = 0

    @property
    def saved_tokens(self) -> int:
        return max(0, self.before_tokens - self.after_tokens)


def compact_old_tool_results_by_id(
    messages: list[LLMMessage],
    *,
    replacements: dict[str, str],
    token_estimator: Callable[[str], int],
    config: ToolResultCacheEditConfig = ToolResultCacheEditConfig(),
) -> tuple[list[LLMMessage], ToolResultCacheEditStats]:
    """Replace old tool results with stable per-tool_call_id cache entries.

    Provider tool-call validity depends on keeping the assistant tool call and
    the matching tool result in the transcript. We therefore never remove the
    message; only its content is replaced by a deterministic compact entry.
    """

    tool_indices = [
        index
        for index, message in enumerate(messages)
        if message.role == "tool" and str(message.tool_call_id or "").strip()
    ]
    if len(tool_indices) <= max(0, config.keep_recent):
        return messages, ToolResultCacheEditStats()

    recent = set(tool_indices[-max(0, config.keep_recent):]) if config.keep_recent else set()
    preserved_unconsumed = _unconsumed_tool_batch_result_indices(messages) if config.preserve_unconsumed_tool_batch else set()
    compacted = 0
    before_tokens = 0
    after_tokens = 0
    edited = list(messages)

    for index in tool_indices:
        if index in recent or index in preserved_unconsumed:
            continue
        message = messages[index]
        content = str(message.content or "")
        if len(content) < config.min_chars or is_tool_result_cache_entry(content):
            continue

        call_id = str(message.tool_call_id or "").strip()
        replacement = replacements.get(call_id)
        if replacement is None:
            replacement = build_tool_result_cache_entry(
                tool_call_id=call_id,
                tool_name=message.name or "unknown",
                content=content,
                preview_chars=config.preview_chars,
                estimated_tokens=token_estimator(content),
            )
            replacements[call_id] = replacement

        before_tokens += token_estimator(content)
        after_tokens += token_estimator(replacement)
        edited[index] = LLMMessage(
            role=message.role,
            content=replacement,
            name=message.name,
            tool_call_id=message.tool_call_id,
            tool_calls=message.tool_calls,
            images=list(message.images),
            documents=list(message.documents),
        )
        compacted += 1

    if compacted == 0:
        return messages, ToolResultCacheEditStats()

    return edited, ToolResultCacheEditStats(
        compacted=compacted,
        before_tokens=before_tokens,
        after_tokens=after_tokens,
    )


def _unconsumed_tool_batch_result_indices(messages: list[LLMMessage]) -> set[int]:
    """Return tool results from the latest assistant tool batch not yet consumed.

    While a loop is between tool execution and the follow-up model call, history
    ends with the tool result messages for the assistant's latest tool_calls.
    Those fresh results are the evidence the next model call needs, so cache
    editing must not replace them with previews yet. Once any later user/system
    or assistant text appears, the batch has been consumed and can compact like
    older history.
    """
    last_tool_call_assistant = -1
    expected_ids: set[str] = set()
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message.role != "assistant" or not message.tool_calls:
            continue
        last_tool_call_assistant = index
        expected_ids = {
            str(tool_call.id)
            for tool_call in message.tool_calls
            if str(getattr(tool_call, "id", "") or "").strip()
        }
        break

    if last_tool_call_assistant < 0 or not expected_ids:
        return set()

    suffix = messages[last_tool_call_assistant + 1:]
    if not suffix or any(message.role != "tool" for message in suffix):
        return set()

    return {
        last_tool_call_assistant + 1 + offset
        for offset, message in enumerate(suffix)
        if str(message.tool_call_id or "").strip() in expected_ids
    }


def is_tool_result_cache_entry(content: str) -> bool:
    return content.lstrip().startswith(TOOL_RESULT_CACHE_ENTRY_PREFIX)


def build_tool_result_cache_entry(
    *,
    tool_call_id: str,
    tool_name: str,
    content: str,
    preview_chars: int,
    estimated_tokens: int,
) -> str:
    original_chars = len(content)
    content_hash = hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()[:12]
    line_count = content.count("\n") + (1 if content else 0)
    head_size = max(0, int(preview_chars * 0.62))
    tail_size = max(0, preview_chars - head_size)
    omitted = max(0, original_chars - head_size - tail_size)
    preview = content if omitted <= 0 else (
        f"{content[:head_size]}\n"
        f"... [{omitted} chars omitted from old tool result {tool_call_id}] ...\n"
        f"{content[-tail_size:]}"
    )
    return (
        f'{TOOL_RESULT_CACHE_ENTRY_PREFIX} '
        f'tool_use_id="{escape(tool_call_id, quote=True)}" '
        f'name="{escape(tool_name, quote=True)}" '
        f'original_chars="{original_chars}" '
        f'estimated_tokens="{max(0, int(estimated_tokens))}" '
        f'content_hash="{content_hash}" '
        f'line_count="{max(0, int(line_count))}">\n'
        "Full output was removed from active context to preserve prompt-cache reuse and reduce token spend. "
        "If the exact old output is required, rerun a narrower tool call or read the referenced artifact when one is present.\n"
        "--- retained preview ---\n"
        f"{preview}\n"
        f"{TOOL_RESULT_CACHE_ENTRY_SUFFIX}"
    )
