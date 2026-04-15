"""
Agent 状态管理（DESIGN.md §7 Level 4 AgentState）。

AgentState 是 Agent Loop 的运行时状态载体，包含：
  - 任务进度
  - 工具调用历史
  - Artifact 引用
  - 活跃 Skill
  - 停滞检测数据
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class ToolCallRecord:
    """单次工具调用记录。"""

    tool_name: str
    tool_input: dict[str, Any] = field(default_factory=dict)
    tool_output: str | None = None
    artifact_id: str | None = None
    status: Literal["success", "error"] = "success"


@dataclass
class AgentState:
    """
    Agent 运行时状态。

    生命周期：一次用户消息处理过程。
    """

    user_message: str
    max_iterations: int = 30

    # ── 执行状态 ──
    iterations: int = 0
    reply: str = ""
    stopped_reason: Literal[
        "completed",
        "tool_error",
        "invalid_model_action",
        "max_iterations",
        "stagnation",
        "budget_exceeded",
        "interrupted",
        "api_error",
    ] | None = None

    # ── 工具记录 ──
    tool_calls: list[ToolCallRecord] = field(default_factory=list)

    # ── 上下文增强（Phase 2 新增）──
    active_skills: list[str] = field(default_factory=list)
    retrieved_chunks: list[str] = field(default_factory=list)
    task_summary: str = ""
    artifact_refs: list[str] = field(default_factory=list)

    # ── 停滞检测数据 ──
    _tool_call_hashes: dict[str, int] = field(default_factory=dict)

    def record_tool_call(
        self,
        name: str,
        args: dict[str, Any],
        output: str,
        artifact_id: str | None = None,
        is_error: bool = False,
    ) -> None:
        """记录一次工具调用。"""
        self.tool_calls.append(
            ToolCallRecord(
                tool_name=name,
                tool_input=args,
                tool_output=output,
                artifact_id=artifact_id,
                status="error" if is_error else "success",
            )
        )
        if artifact_id:
            self.artifact_refs.append(artifact_id)

        # 停滞检测：记录 hash
        call_hash = self._hash_call(name, args)
        self._tool_call_hashes[call_hash] = (
            self._tool_call_hashes.get(call_hash, 0) + 1
        )

    def is_stagnant(self, limit: int = 3) -> bool:
        """
        检测是否停滞（DESIGN.md §7 终止条件）。

        同一工具+同一参数调用 ≥ limit 次判定停滞。
        """
        return any(count >= limit for count in self._tool_call_hashes.values())

    def get_stagnation_detail(self) -> str:
        """获取停滞的详细信息。"""
        for call_hash, count in self._tool_call_hashes.items():
            if count >= 3:
                return f"相同工具调用已重复 {count} 次"
        return ""

    @staticmethod
    def _hash_call(name: str, args: dict[str, Any]) -> str:
        """生成工具调用的 hash（用于去重检测）。"""
        raw = f"{name}:{json.dumps(args, sort_keys=True, ensure_ascii=False)}"
        return hashlib.md5(raw.encode()).hexdigest()[:12]
