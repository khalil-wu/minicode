"""
上下文构建器

根据 newplan.md 第 12 节实现
六段式上下文构建 + 预算管理 + 压缩策略
"""

from typing import List
from .models import (
    ContextSegment,
    ContextSegmentType,
    ContextBudget,
    ContextSnapshot,
)


class ContextBuilder:
    """上下文构建器"""

    def __init__(self, total_tokens: int = 200000):
        self.budget = ContextBudget.from_total(total_tokens)
        self.segments: List[ContextSegment] = []

    def add_instruction(self, content: str, token_count: int) -> None:
        """添加指令段落"""
        self.segments.append(
            ContextSegment(
                type=ContextSegmentType.INSTRUCTION,
                content=content,
                token_count=token_count,
                priority=100,  # 最高优先级
            )
        )

    def add_tools(self, content: str, token_count: int, tool_names: List[str] = None) -> None:
        """添加工具定义段落"""
        self.segments.append(
            ContextSegment(
                type=ContextSegmentType.TOOLS,
                content=content,
                token_count=token_count,
                priority=90,
                metadata={"tool_names": tool_names or []},
            )
        )

    def add_memory(self, content: str, token_count: int, memory_type: str = "file") -> None:
        """添加记忆段落"""
        self.segments.append(
            ContextSegment(
                type=ContextSegmentType.MEMORY,
                content=content,
                token_count=token_count,
                priority=85,
                metadata={"memory_type": memory_type},
            )
        )

    def add_runtime_state(self, content: str, token_count: int) -> None:
        """添加运行时状态段落"""
        self.segments.append(
            ContextSegment(
                type=ContextSegmentType.RUNTIME_STATE,
                content=content,
                token_count=token_count,
                priority=80,
            )
        )

    def add_retrieval(self, content: str, token_count: int, relevance_score: float = 0.0) -> None:
        """添加检索结果段落"""
        self.segments.append(
            ContextSegment(
                type=ContextSegmentType.RETRIEVAL,
                content=content,
                token_count=token_count,
                priority=70,
                metadata={"relevance_score": relevance_score},
            )
        )

    def add_conversation(self, content: str, token_count: int, message_index: int = 0) -> None:
        """添加对话历史段落"""
        self.segments.append(
            ContextSegment(
                type=ContextSegmentType.CONVERSATION,
                content=content,
                token_count=token_count,
                priority=60,
                metadata={"message_index": message_index},
            )
        )

    def add_current_request(self, content: str, token_count: int) -> None:
        """添加当前请求段落"""
        self.segments.append(
            ContextSegment(
                type=ContextSegmentType.CURRENT_REQUEST,
                content=content,
                token_count=token_count,
                priority=95,  # 高优先级
            )
        )

    def build(self, apply_compaction: bool = True) -> ContextSnapshot:
        """构建上下文快照"""
        total_tokens = sum(seg.token_count for seg in self.segments)
        compaction_occurred = False
        compaction_reason = None

        # 如果超出预算，执行压缩
        if apply_compaction and total_tokens > self.budget.total_tokens:
            self.segments, compaction_reason = self._compact()
            total_tokens = sum(seg.token_count for seg in self.segments)
            compaction_occurred = True

        return ContextSnapshot(
            segments=self.segments,
            total_tokens=total_tokens,
            budget=self.budget,
            compaction_occurred=compaction_occurred,
            compaction_reason=compaction_reason,
        )

    def _compact(self) -> tuple[List[ContextSegment], str]:
        """压缩上下文"""
        # 压缩顺序：
        # 1. 长工具结果
        # 2. 早期对话历史
        # 3. 重复状态摘要

        compacted_segments = self.segments.copy()
        reason_parts = []

        # 1. 压缩长工具结果（超过 5000 tokens 的工具输出）
        tool_segments = [s for s in compacted_segments if s.type == ContextSegmentType.TOOLS]
        for seg in tool_segments:
            if seg.token_count > 5000:
                # 截断到 2000 tokens
                seg.content = seg.content[:8000] + "\n\n[... truncated ...]"
                seg.token_count = 2000
                reason_parts.append("long tool results")

        # 2. 压缩早期对话历史（保留最近 20 条）
        conversation_segments = [
            s for s in compacted_segments if s.type == ContextSegmentType.CONVERSATION
        ]
        conversation_segments.sort(key=lambda s: s.metadata.get("message_index", 0))

        if len(conversation_segments) > 20:
            # 移除早期对话
            to_remove = conversation_segments[:-20]
            for seg in to_remove:
                compacted_segments.remove(seg)
            reason_parts.append("early conversation history")

        # 3. 去重运行时状态（只保留最新的）
        runtime_segments = [
            s for s in compacted_segments if s.type == ContextSegmentType.RUNTIME_STATE
        ]
        if len(runtime_segments) > 1:
            # 只保留最后一个
            for seg in runtime_segments[:-1]:
                compacted_segments.remove(seg)
            reason_parts.append("duplicate runtime state")

        # 4. 如果还超出预算，按优先级截断检索结果
        total_tokens = sum(s.token_count for s in compacted_segments)
        if total_tokens > self.budget.total_tokens:
            retrieval_segments = [
                s for s in compacted_segments if s.type == ContextSegmentType.RETRIEVAL
            ]
            retrieval_segments.sort(
                key=lambda s: s.metadata.get("relevance_score", 0.0), reverse=True
            )

            # 计算可用预算
            _, max_retrieval = self.budget.get_segment_budget(ContextSegmentType.RETRIEVAL)
            current_retrieval_tokens = sum(s.token_count for s in retrieval_segments)

            if current_retrieval_tokens > max_retrieval:
                # 截断低相关性检索结果
                kept_tokens = 0
                kept_segments = []
                for seg in retrieval_segments:
                    if kept_tokens + seg.token_count <= max_retrieval:
                        kept_segments.append(seg)
                        kept_tokens += seg.token_count
                    else:
                        compacted_segments.remove(seg)

                reason_parts.append("low-relevance retrieval results")

        reason = ", ".join(reason_parts) if reason_parts else "budget exceeded"
        return compacted_segments, reason

    def get_segment_summary(self) -> dict:
        """获取段落摘要"""
        summary = {}
        for segment_type in ContextSegmentType:
            segments = [s for s in self.segments if s.type == segment_type]
            summary[segment_type.value] = {
                "count": len(segments),
                "total_tokens": sum(s.token_count for s in segments),
            }
        return summary

    def clear(self) -> None:
        """清空所有段落"""
        self.segments.clear()
