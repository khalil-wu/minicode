"""
上下文模型定义

根据 newplan.md 第 12 节实现
六段式上下文模型 + 预算管理
"""

from dataclasses import dataclass, field
from typing import List, Optional, Dict
from enum import Enum


class ContextSegmentType(str, Enum):
    """上下文段落类型"""
    INSTRUCTION = "instruction"  # 指令
    TOOLS = "tools"  # 工具定义
    MEMORY = "memory"  # 记忆
    RUNTIME_STATE = "runtime_state"  # 运行时状态
    RETRIEVAL = "retrieval"  # 检索结果
    CONVERSATION = "conversation"  # 对话历史
    CURRENT_REQUEST = "current_request"  # 当前请求


@dataclass
class ContextSegment:
    """上下文段落"""
    type: ContextSegmentType
    content: str
    token_count: int
    priority: int = 0  # 优先级（越高越重要）
    metadata: Dict = field(default_factory=dict)


@dataclass
class ContextBudget:
    """上下文预算"""
    total_tokens: int
    instruction_min: int  # 指令最小 token 数
    instruction_max: int  # 指令最大 token 数
    tools_min: int
    tools_max: int
    memory_min: int
    memory_max: int
    runtime_state_min: int
    runtime_state_max: int
    retrieval_min: int
    retrieval_max: int
    conversation_min: int
    conversation_max: int
    current_request_min: int
    current_request_max: int

    @classmethod
    def from_total(cls, total_tokens: int) -> "ContextBudget":
        """根据总 token 数创建预算"""
        return cls(
            total_tokens=total_tokens,
            # 指令：5-8%
            instruction_min=int(total_tokens * 0.05),
            instruction_max=int(total_tokens * 0.08),
            # 工具：10-15%
            tools_min=int(total_tokens * 0.10),
            tools_max=int(total_tokens * 0.15),
            # 记忆：5-10%
            memory_min=int(total_tokens * 0.05),
            memory_max=int(total_tokens * 0.10),
            # 运行时状态：3-5%
            runtime_state_min=int(total_tokens * 0.03),
            runtime_state_max=int(total_tokens * 0.05),
            # 检索结果：10-20%
            retrieval_min=int(total_tokens * 0.10),
            retrieval_max=int(total_tokens * 0.20),
            # 对话历史：40-55%
            conversation_min=int(total_tokens * 0.40),
            conversation_max=int(total_tokens * 0.55),
            # 当前请求：5-10%
            current_request_min=int(total_tokens * 0.05),
            current_request_max=int(total_tokens * 0.10),
        )

    def get_segment_budget(self, segment_type: ContextSegmentType) -> tuple[int, int]:
        """获取段落预算（最小值，最大值）"""
        if segment_type == ContextSegmentType.INSTRUCTION:
            return self.instruction_min, self.instruction_max
        elif segment_type == ContextSegmentType.TOOLS:
            return self.tools_min, self.tools_max
        elif segment_type == ContextSegmentType.MEMORY:
            return self.memory_min, self.memory_max
        elif segment_type == ContextSegmentType.RUNTIME_STATE:
            return self.runtime_state_min, self.runtime_state_max
        elif segment_type == ContextSegmentType.RETRIEVAL:
            return self.retrieval_min, self.retrieval_max
        elif segment_type == ContextSegmentType.CONVERSATION:
            return self.conversation_min, self.conversation_max
        elif segment_type == ContextSegmentType.CURRENT_REQUEST:
            return self.current_request_min, self.current_request_max
        return 0, 0


@dataclass
class ContextSnapshot:
    """上下文快照"""
    segments: List[ContextSegment]
    total_tokens: int
    budget: ContextBudget
    compaction_occurred: bool = False
    compaction_reason: Optional[str] = None

    def get_segment_tokens(self, segment_type: ContextSegmentType) -> int:
        """获取指定段落的 token 数"""
        return sum(
            seg.token_count
            for seg in self.segments
            if seg.type == segment_type
        )

    def get_usage_percentage(self) -> float:
        """获取使用率百分比"""
        return (self.total_tokens / self.budget.total_tokens) * 100 if self.budget.total_tokens > 0 else 0

    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            "total_tokens": self.total_tokens,
            "budget_tokens": self.budget.total_tokens,
            "usage_percentage": round(self.get_usage_percentage(), 2),
            "compaction_occurred": self.compaction_occurred,
            "compaction_reason": self.compaction_reason,
            "segments": {
                "instruction": self.get_segment_tokens(ContextSegmentType.INSTRUCTION),
                "tools": self.get_segment_tokens(ContextSegmentType.TOOLS),
                "memory": self.get_segment_tokens(ContextSegmentType.MEMORY),
                "runtime_state": self.get_segment_tokens(ContextSegmentType.RUNTIME_STATE),
                "retrieval": self.get_segment_tokens(ContextSegmentType.RETRIEVAL),
                "conversation": self.get_segment_tokens(ContextSegmentType.CONVERSATION),
                "current_request": self.get_segment_tokens(ContextSegmentType.CURRENT_REQUEST),
            },
        }


@dataclass
class SkillMetadata:
    """Skill 元数据"""
    id: str
    name: str
    description: str
    is_active: bool = False
    activation_trigger: Optional[str] = None  # "explicit", "auto", "rule"
    token_count: int = 0

    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "is_active": self.is_active,
            "activation_trigger": self.activation_trigger,
            "token_count": self.token_count,
        }
