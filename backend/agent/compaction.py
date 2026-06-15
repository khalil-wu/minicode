"""
上下文自动压缩系统 (Context Compaction)

扩展现有的 compaction 功能，添加自动触发和多种压缩模式。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from backend.llm.base import LLMMessage

logger = logging.getLogger(__name__)


class CompactMode(Enum):
    """压缩模式"""
    SNIP = "snip"  # 保留最近 N 轮
    SUMMARIZE = "summarize"  # LLM 总结（现有实现）
    MICRO_COMPACT = "micro_compact"  # 合并工具调用


@dataclass(frozen=True)
class CompactionOutput:
    summary: str
    memdir_facts: list[str] = field(default_factory=list)


@dataclass
class CompactionResult:
    """压缩结果统计"""
    before_tokens: int
    after_tokens: int
    mode: CompactMode
    messages_removed: int
    messages_kept: int
    summary: str = ""


def format_compaction_history(messages: list[LLMMessage]) -> str:
    """Format conversation history for LLM-based compaction.

    Strategy: include all user/assistant messages (truncated for very long ones),
    and summarise tool results to keep context without flooding tokens.
    """
    parts: list[str] = []
    for message in messages:
        if message.role == "user":
            content = message.content or ""
            # Keep up to 600 chars for user messages — they carry intent
            parts.append(f"User: {content[:600]}{'...' if len(content) > 600 else ''}")
        elif message.role == "assistant" and message.content:
            content = message.content
            parts.append(f"Assistant: {content[:400]}{'...' if len(content) > 400 else ''}")
        elif message.role == "tool":
            content = message.content or ""
            # Tool results: keep first 200 chars — enough to see what was returned
            parts.append(f"Tool({message.name}): {content[:200]}{'...' if len(content) > 200 else ''}")
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


# ══════════════════════════════════════════════════════════════════
# 新增：自动压缩控制器
# ══════════════════════════════════════════════════════════════════


class ContextCompactor:
    """上下文自动压缩器

    在 token 使用率超过阈值时自动压缩消息历史。
    支持三种模式：Snip（快）、Summarize（准）、Micro-compact（增量）
    """

    def __init__(
        self,
        llm: Any,
        token_budget: Any,
        threshold: float = 0.80,
    ):
        self.llm = llm
        self.token_budget = token_budget
        self.threshold = threshold
        self._compaction_count = 0

    def should_compact(self, messages: list[dict]) -> bool:
        """判断是否需要压缩"""
        current_tokens = self._estimate_tokens(messages)
        usage_ratio = current_tokens / self.token_budget.total

        should = usage_ratio > self.threshold
        if should:
            logger.info(
                f"Context compaction triggered: {current_tokens}/{self.token_budget.total} "
                f"({usage_ratio:.1%})"
            )
        return should

    async def compact(
        self,
        messages: list[dict],
        mode: CompactMode = CompactMode.SNIP,
        keep_recent_turns: int = 5,
    ) -> tuple[list[dict], CompactionResult]:
        """压缩消息历史"""
        before_tokens = self._estimate_tokens(messages)

        if mode == CompactMode.SNIP:
            compacted, removed = await self._snip_mode(messages, keep_recent_turns)
        elif mode == CompactMode.SUMMARIZE:
            compacted, removed = await self._summarize_mode(messages, keep_recent_turns)
        elif mode == CompactMode.MICRO_COMPACT:
            compacted, removed = await self._micro_compact(messages)
        else:
            compacted, removed = messages, 0

        after_tokens = self._estimate_tokens(compacted)
        self._compaction_count += 1

        result = CompactionResult(
            before_tokens=before_tokens,
            after_tokens=after_tokens,
            mode=mode,
            messages_removed=removed,
            messages_kept=len(compacted),
        )

        logger.info(
            f"Compaction #{self._compaction_count}: "
            f"{before_tokens} → {after_tokens} tokens "
            f"({(1 - after_tokens/before_tokens)*100:.1f}% reduction)"
        )

        return compacted, result

    async def _snip_mode(
        self, messages: list[dict], keep_recent_turns: int
    ) -> tuple[list[dict], int]:
        """Snip: 保留最近 N 轮 + 边界标记"""
        system_msgs = [m for m in messages if m.get("role") == "system"]
        non_system = [m for m in messages if m.get("role") != "system"]

        recent_count = keep_recent_turns * 2
        if len(non_system) <= recent_count:
            return messages, 0

        recent_msgs = non_system[-recent_count:]
        removed_count = len(non_system) - recent_count

        snip_marker = {
            "role": "system",
            "content": (
                f"[Earlier conversation compressed: {removed_count} messages summarized. "
                f"Following are the most recent {keep_recent_turns} exchanges.]"
            ),
        }

        return system_msgs + [snip_marker] + recent_msgs, removed_count

    async def _summarize_mode(
        self, messages: list[dict], keep_recent_turns: int
    ) -> tuple[list[dict], int]:
        """Summarize: LLM 总结历史（使用现有实现）"""
        system_msgs = [m for m in messages if m.get("role") == "system"]
        non_system = [m for m in messages if m.get("role") != "system"]

        recent_count = keep_recent_turns * 2
        if len(non_system) <= recent_count + 2:
            return messages, 0

        to_summarize = non_system[:-recent_count]
        recent_msgs = non_system[-recent_count:]

        # 使用现有的 format_compaction_history
        formatted = format_compaction_history(
            [self._dict_to_llm_message(m) for m in to_summarize]
        )

        summary_prompt = (
            "Summarize this conversation concisely. Focus on: "
            "key decisions, important findings, context for continuation.\n\n"
            f"{formatted}"
        )

        try:
            from backend.llm.base import LLMAdapter
            if isinstance(self.llm, LLMAdapter):
                response = await self.llm.generate(summary_prompt, max_tokens=500)
                summary = response.strip()
            else:
                # Fallback to snip if LLM unavailable
                return await self._snip_mode(messages, keep_recent_turns)
        except Exception as e:
            logger.warning(f"Summarization failed, using snip: {e}")
            return await self._snip_mode(messages, keep_recent_turns)

        summary_msg = {
            "role": "system",
            "content": f"[Conversation summary]\n\n{summary}",
        }

        return system_msgs + [summary_msg] + recent_msgs, len(to_summarize)

    async def _micro_compact(self, messages: list[dict]) -> tuple[list[dict], int]:
        """Micro-compact: 合并相似的工具调用"""
        compacted = []
        current_batch: list[dict] = []
        removed = 0

        for msg in messages:
            if msg.get("role") == "assistant" and "tool_calls" in msg:
                current_batch.append(msg)
            else:
                if current_batch:
                    merged = self._merge_tool_calls(current_batch)
                    compacted.append(merged)
                    removed += len(current_batch) - 1
                    current_batch = []
                compacted.append(msg)

        if current_batch:
            merged = self._merge_tool_calls(current_batch)
            compacted.append(merged)
            removed += len(current_batch) - 1

        return compacted, removed

    def _merge_tool_calls(self, messages: list[dict]) -> dict:
        """合并多个工具调用消息"""
        if len(messages) == 1:
            return messages[0]

        all_tool_calls = []
        for msg in messages:
            all_tool_calls.extend(msg.get("tool_calls", []))

        return {
            "role": "assistant",
            "content": f"[Merged {len(messages)} tool calls]",
            "tool_calls": all_tool_calls,
        }

    def _estimate_tokens(self, messages: list[dict]) -> int:
        """估算 token 数量（简单启发式）"""
        total_chars = 0
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                total_chars += len(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and "text" in block:
                        total_chars += len(block["text"])

        # 3 chars ≈ 1 token + 20 tokens overhead per message
        return total_chars // 3 + len(messages) * 20

    def _dict_to_llm_message(self, msg: dict) -> LLMMessage:
        """转换 dict 为 LLMMessage（用于现有函数）"""
        return LLMMessage(
            role=msg.get("role", "user"),
            content=msg.get("content", ""),
            name=msg.get("name"),
        )

    def get_stats(self) -> dict[str, Any]:
        """获取统计信息"""
        return {
            "compaction_count": self._compaction_count,
            "threshold": self.threshold,
            "budget_total": self.token_budget.total,
        }

