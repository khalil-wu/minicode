"""
API-level context edit / cached microcompact.

Mirrors Claude Code's API-level strategy of stripping thinking blocks and
compacting old tool use/tool result pairs from the message list *before*
sending to the provider API, while preserving the conversation structure.

Two complementary strategies are applied:

1. **Thinking strip**: Remove extended-thinking content from older assistant
   messages (keep the most recent N).  Thinking blocks are useful for the
   model's reasoning chain but consume significant tokens on subsequent turns.
   Anthropic's API supports sending `thinking` blocks back for context, but
   once they are more than a few turns old the benefit is negligible.

2. **Tool-use microcompact**: For old tool-call/tool-result pairs that have
   already been consumed (i.e., a later assistant text message exists),
   replace the verbose tool result content with a compact summary while
   keeping the message structure intact (assistant tool_calls + matching
   tool results must remain paired for API validity).

3. **Provider-items strip**: Optionally remove `provider_items` (reasoning
   blocks, intermediate function calls) from old assistant messages. This is
   disabled by default because stateless provider replay may need those items.

This module operates on `list[LLMMessage]` and returns a new list; it does
not mutate the ContextBuilder's internal history.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from backend.llm.base import LLMMessage

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ContextEditConfig:
    """Configuration for API-level context editing."""

    # Number of recent assistant messages whose thinking blocks are preserved.
    # Older thinking blocks are stripped to save tokens.
    keep_recent_thinking: int = 2

    # Number of recent tool results whose full content is preserved.
    # Older tool results are replaced with compact summaries.
    keep_recent_tool_results: int = 4

    # Minimum character count for a tool result to be eligible for compaction.
    # Short results are left as-is.
    min_tool_result_chars: int = 400

    # Whether to strip provider_items (reasoning/function_call artifacts)
    # from old assistant messages.
    strip_old_provider_items: bool = False

    # Whether to compact old tool results here. When the ContextBuilder already
    # ran its history-level compactor (compact_old_tool_results_by_id), this is
    # redundant work on the same messages and would double-wrap the resulting
    # <tool_result_cache_entry> blocks, so the caller disables it. It stays on
    # for stateful-history mode, where the history compactor is skipped and this
    # is the only tool-result compaction path.
    compact_tool_results: bool = True

    # Number of recent assistant messages whose provider_items are preserved.
    keep_recent_provider_items: int = 2

    # Whether to strip thinking blocks entirely (vs replacing with a
    # placeholder). When True, the thinking content is removed; the
    # provider sees no thinking block for that message.
    strip_thinking_completely: bool = True

    # Preview chars for compacted tool results
    tool_result_preview_chars: int = 200


@dataclass
class ContextEditStats:
    """Statistics returned after context editing."""

    thinking_stripped: int = 0
    tool_results_compacted: int = 0
    provider_items_stripped: int = 0
    before_tokens: int = 0
    after_tokens: int = 0

    @property
    def saved_tokens(self) -> int:
        return max(0, self.before_tokens - self.after_tokens)

    @property
    def total_edits(self) -> int:
        return self.thinking_stripped + self.tool_results_compacted + self.provider_items_stripped


def apply_context_edit(
    messages: list[LLMMessage],
    *,
    config: ContextEditConfig = ContextEditConfig(),
    token_estimator: Callable[[str], int] | None = None,
) -> tuple[list[LLMMessage], ContextEditStats]:
    """Apply API-level context editing to a message list.

    Returns a new message list with thinking blocks and old tool results
    compacted. The original list is not mutated.

    The editing is safe for provider APIs:
    - Assistant tool_calls messages are never removed; only their content
      and provider_items may be stripped.
    - Tool result messages always keep their tool_call_id and role.
    - The pairing of assistant tool_calls + tool results is preserved.
    """
    if not messages:
        return messages, ContextEditStats()

    estimate = token_estimator or _default_token_estimator
    stats = ContextEditStats()
    stats.before_tokens = sum(estimate(str(m.content or "")) for m in messages)

    edited = list(messages)

    # ── Phase 1: Index assistant messages ────────────────────────────
    assistant_indices = [
        i for i, m in enumerate(edited)
        if m.role == "assistant"
    ]
    total_assistant = len(assistant_indices)

    # Messages whose thinking/provider_items should be preserved
    recent_assistant = set(
        assistant_indices[-config.keep_recent_thinking:]
        if config.keep_recent_thinking > 0 and assistant_indices
        else []
    )
    recent_provider_assistant = set(
        assistant_indices[-config.keep_recent_provider_items:]
        if config.keep_recent_provider_items > 0 and assistant_indices
        else []
    )

    # ── Phase 2: Strip thinking blocks from old assistant messages ──
    for idx in assistant_indices:
        if idx in recent_assistant:
            continue
        msg = edited[idx]
        content = str(msg.content or "")

        # Strip thinking blocks: content between <thinking>...</thinking>
        # or <antThinking>...</antThinking> tags
        new_content = _strip_thinking_blocks(content)
        if new_content != content:
            stats.thinking_stripped += 1
            edited[idx] = _replace_content(msg, new_content)

    # ── Phase 3: Strip provider_items from old assistant messages ────
    if config.strip_old_provider_items:
        for idx in assistant_indices:
            if idx in recent_provider_assistant:
                continue
            msg = edited[idx]
            if msg.provider_items:
                stats.provider_items_stripped += 1
                edited[idx] = _replace_provider_items(msg, [])

    # ── Phase 4: Compact old tool results ────────────────────────────
    tool_indices = [
        i for i, m in enumerate(edited)
        if m.role == "tool" and str(m.tool_call_id or "").strip()
    ]

    if config.compact_tool_results and len(tool_indices) > config.keep_recent_tool_results:
        recent_tool = (
            set(tool_indices[-config.keep_recent_tool_results:])
            if config.keep_recent_tool_results > 0
            else set()
        )
        # Also preserve unconsumed tool batch (results at the end of history
        # that haven't been followed by a user/assistant message yet)
        preserved = _unconsumed_tool_batch_indices(edited) | recent_tool
        tool_names_by_id = _tool_call_names_by_id(edited)

        for idx in tool_indices:
            if idx in preserved:
                continue
            msg = edited[idx]
            tool_name = _tool_name_for_result(msg, tool_names_by_id)
            if tool_name not in COMPACTABLE_TOOL_NAMES:
                continue
            content = str(msg.content or "")
            if len(content) < config.min_tool_result_chars:
                continue
            # Skip already-compacted entries
            if _is_compact_entry(content):
                continue

            compacted = _compact_tool_result(
                content,
                tool_call_id=str(msg.tool_call_id or ""),
                tool_name=tool_name,
                preview_chars=config.tool_result_preview_chars,
            )
            stats.tool_results_compacted += 1
            edited[idx] = _replace_content(msg, compacted)

    # ── Phase 5: Recalculate token estimate ──────────────────────────
    stats.after_tokens = sum(estimate(str(m.content or "")) for m in edited)

    if stats.total_edits == 0:
        return messages, stats

    return edited, stats


# ── Internal helpers ────────────────────────────────────────────────

import re

_THINKING_PATTERNS = [
    re.compile(r"<thinking>.*?</thinking>", re.DOTALL | re.IGNORECASE),
    re.compile(r"<antThinking>.*?</antThinking>", re.DOTALL | re.IGNORECASE),
    re.compile(r"<reasoning>.*?</reasoning>", re.DOTALL | re.IGNORECASE),
]

_COMPACT_PREFIX = "<context_edit_compact"

# Outputs produced by the ContextBuilder's other compaction paths. Content that
# already starts with one of these is fully compacted; re-wrapping it here would
# double-compact and bloat the entry, so it is skipped.
_ALREADY_COMPACT_PREFIXES = (
    _COMPACT_PREFIX,
    "<tool_result_cache_entry",
    "<persisted-tool-result",
)

# Only compact outputs that are safe to recover by re-reading current workspace
# state or re-running a read-style query. Exclude task/read_artifact/team/swarm
# outputs because they may be one-off conversation state.
COMPACTABLE_TOOL_NAMES = frozenset({
    "read_file",
    "list_files",
    "grep_files",
    "glob_files",
    "fuzzy_search",
    "git_status",
    "git_diff",
    "git_log",
    "web_fetch",
    "web_search",
    "go_to_definition",
    "find_references",
    "lsp_go_to_definition",
    "lsp_find_references",
    "lsp_hover",
    "lsp_document_symbols",
})


def _strip_thinking_blocks(content: str) -> str:
    """Remove thinking/reasoning XML blocks from assistant content.

    Only ever called on assistant messages (Phase 2 iterates assistant_indices),
    so tool-result *messages* are never passed in. As an extra guard, a matched
    block is preserved if its body carries tool-result markers — this covers the
    case where an assistant echoed tool output that itself contains a generic
    ``<reasoning>`` tag, so the real payload is not silently deleted.
    """

    def _replace(match: "re.Match[str]") -> str:
        body = match.group(0)
        if "<function_call_result" in body or "<tool_result" in body:
            return body
        return ""

    result = content
    for pattern in _THINKING_PATTERNS:
        result = pattern.sub(_replace, result)
    # Clean up extra whitespace left behind
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip()


def _is_compact_entry(content: str) -> bool:
    return content.lstrip().startswith(_ALREADY_COMPACT_PREFIXES)


def _compact_tool_result(
    content: str,
    *,
    tool_call_id: str,
    tool_name: str,
    preview_chars: int,
) -> str:
    """Replace a verbose tool result with a compact summary entry."""
    original_chars = len(content)
    head_size = max(0, int(preview_chars * 0.62))
    tail_size = max(0, preview_chars - head_size)
    omitted = max(0, original_chars - head_size - tail_size)

    if omitted <= 0:
        return content

    preview = (
        f"{content[:head_size]}\n"
        f"... [{omitted} chars omitted from old tool result {tool_call_id}] ...\n"
        f"{content[-tail_size:]}"
    )

    return (
        f'{_COMPACT_PREFIX} tool_call_id="{tool_call_id}" '
        f'name="{tool_name}" '
        f'original_chars="{original_chars}">\n'
        f"Full output was removed by API-level context edit to reduce token spend. "
        f"If the exact content is needed, re-read the current source or rerun a safe read-only query.\n"
        f"--- retained preview ---\n"
        f"{preview}\n"
        f"</context_edit_compact>"
    )


def _unconsumed_tool_batch_indices(messages: list[LLMMessage]) -> set[int]:
    """Find tool results from the latest assistant tool batch not yet consumed."""
    last_tool_call_idx = -1
    expected_ids: set[str] = set()

    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if msg.role != "assistant" or not msg.tool_calls:
            continue
        last_tool_call_idx = i
        expected_ids = {
            str(tc.id)
            for tc in msg.tool_calls
            if str(getattr(tc, "id", "") or "").strip()
        }
        break

    if last_tool_call_idx < 0 or not expected_ids:
        return set()

    suffix = messages[last_tool_call_idx + 1:]
    # If there are any non-tool messages after the tool results, the batch
    # has been consumed and can be compacted.
    if any(m.role != "tool" for m in suffix):
        return set()

    return {
        last_tool_call_idx + 1 + offset
        for offset, msg in enumerate(suffix)
        if str(msg.tool_call_id or "").strip() in expected_ids
    }


def _tool_call_names_by_id(messages: list[LLMMessage]) -> dict[str, str]:
    names: dict[str, str] = {}
    for msg in messages:
        if msg.role != "assistant" or not msg.tool_calls:
            continue
        for tool_call in msg.tool_calls:
            call_id = str(getattr(tool_call, "id", "") or "").strip()
            name = str(getattr(tool_call, "name", "") or "").strip()
            if call_id and name:
                names[call_id] = name
    return names


def _tool_name_for_result(msg: LLMMessage, tool_names_by_id: dict[str, str]) -> str:
    explicit = str(msg.name or "").strip()
    if explicit:
        return explicit
    return tool_names_by_id.get(str(msg.tool_call_id or "").strip(), "")


def _replace_content(msg: LLMMessage, new_content: str) -> LLMMessage:
    """Create a new LLMMessage with replaced content, preserving other fields."""
    return LLMMessage(
        role=msg.role,
        content=new_content,
        name=msg.name,
        tool_call_id=msg.tool_call_id,
        tool_calls=msg.tool_calls,
        phase=msg.phase,
        provider_items=list(msg.provider_items or []),
        images=list(msg.images),
        documents=list(msg.documents),
    )


def _replace_provider_items(msg: LLMMessage, provider_items: list[dict[str, Any]]) -> LLMMessage:
    """Create a new LLMMessage with replaced provider_items."""
    return LLMMessage(
        role=msg.role,
        content=msg.content,
        name=msg.name,
        tool_call_id=msg.tool_call_id,
        tool_calls=msg.tool_calls,
        phase=msg.phase,
        provider_items=provider_items,
        images=list(msg.images),
        documents=list(msg.documents),
    )


def _default_token_estimator(text: str) -> int:
    """Rough token estimate: ~4 chars per token."""
    return max(1, len(text) // 4)
