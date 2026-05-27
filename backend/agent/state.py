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
    source_url: str | None = None
    extraction_status: str | None = None
    content_preview: str | None = None
    evidence_type: str | None = None
    status: Literal["success", "error", "blocked"] = "success"


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
        "timeout",
        "billing",
    ] | None = None

    # ── 工具记录 ──
    tool_calls: list[ToolCallRecord] = field(default_factory=list)

    # ── 上下文增强（Phase 2 新增）──
    active_skills: list[str] = field(default_factory=list)
    retrieved_chunks: list[str] = field(default_factory=list)
    task_summary: str = ""
    artifact_refs: list[str] = field(default_factory=list)
    workspace_context: Any = None  # WorkspaceContext 实例（项目上下文）
    attachments: list[dict[str, Any]] = field(default_factory=list)

    # ── 停滞检测数据 ──
    _tool_call_hashes: dict[str, int] = field(default_factory=dict)
    _tool_call_labels: dict[str, str] = field(default_factory=dict)
    _tool_call_last_index: dict[str, int] = field(default_factory=dict)
    _last_tool_call_hash: str | None = None
    _consecutive_tool_call_count: int = 0
    _tool_sequence: int = 0
    _last_mutation_index: int = 0
    blocked_repeat_calls: int = 0
    heal_attempts: int = 0
    max_heal_attempts: int = 2

    def record_tool_call(
        self,
        name: str,
        args: dict[str, Any],
        output: str,
        artifact_id: str | None = None,
        is_error: bool = False,
        status: Literal["success", "error", "blocked"] | None = None,
        mutates: bool = False,
        source_url: str | None = None,
        extraction_status: str | None = None,
        content_preview: str | None = None,
        evidence_type: str | None = None,
    ) -> None:
        """记录一次工具调用。"""
        resolved_status: Literal["success", "error", "blocked"]
        if status is not None:
            resolved_status = status
        else:
            resolved_status = "error" if is_error else "success"

        self.tool_calls.append(
            ToolCallRecord(
                tool_name=name,
                tool_input=args,
                tool_output=output,
                artifact_id=artifact_id,
                source_url=source_url,
                extraction_status=extraction_status,
                content_preview=content_preview,
                evidence_type=evidence_type,
                status=resolved_status,
            )
        )
        if artifact_id:
            self.artifact_refs.append(artifact_id)

        # 停滞检测：记录 hash
        call_hash = self._hash_call(name, args)
        self._tool_sequence += 1
        self._tool_call_hashes[call_hash] = (
            self._tool_call_hashes.get(call_hash, 0) + 1
        )
        self._tool_call_labels.setdefault(call_hash, self._label_call(name, args))
        self._tool_call_last_index[call_hash] = self._tool_sequence

        if self._last_tool_call_hash == call_hash:
            self._consecutive_tool_call_count += 1
        else:
            self._last_tool_call_hash = call_hash
            self._consecutive_tool_call_count = 1

        if mutates and resolved_status == "success":
            self._last_mutation_index = self._tool_sequence

        if resolved_status == "blocked":
            self.blocked_repeat_calls += 1
        else:
            self.blocked_repeat_calls = 0

    def repeat_count(self, name: str, args: dict[str, Any]) -> int:
        """Return how many times the exact tool call has already been recorded."""
        return self._tool_call_hashes.get(self.call_signature(name, args), 0)

    def call_signature(self, name: str, args: dict[str, Any]) -> str:
        """Stable signature for one tool name + argument set."""
        return self._hash_call(name, args)

    def find_last_tool_call(
        self,
        name: str,
        args: dict[str, Any],
    ) -> ToolCallRecord | None:
        """Find the latest record for the exact same tool name and arguments."""
        call_hash = self._hash_call(name, args)
        for record in reversed(self.tool_calls):
            if self._hash_call(record.tool_name, record.tool_input) == call_hash:
                return record
        return None

    def repeated_call_guard_reason(
        self,
        name: str,
        args: dict[str, Any],
        *,
        limit: int = 3,
    ) -> str:
        """
        Return a PreToolUse-style block reason for wasteful repeated calls.

        This blocks the immediate second identical call, and also blocks the
        third total identical call when no workspace-changing action happened
        since the previous identical call.
        """
        call_hash = self._hash_call(name, args)
        count = self._tool_call_hashes.get(call_hash, 0)
        if count <= 0:
            return ""

        last_call_index = self._tool_call_last_index.get(call_hash, 0)
        if self._last_mutation_index > last_call_index:
            return ""

        consecutive = (
            self._consecutive_tool_call_count
            if self._last_tool_call_hash == call_hash
            else 0
        )
        repeat_threshold = max(limit - 1, 1)
        if consecutive < 1 and count < repeat_threshold:
            return ""

        label = self._tool_call_labels.get(call_hash, self._label_call(name, args))
        return (
            f"Skipped repeated tool call before execution: {label}. "
            f"The same tool and arguments have already been used {count} time(s) "
            "without any workspace-changing action since. Use the previous result, "
            "choose different arguments, or try a different tool/approach."
        )

    def is_stagnant(self, limit: int = 3) -> bool:
        """
        检测是否停滞（DESIGN.md §7 终止条件）。

        同一工具+同一参数调用 ≥ limit 次判定停滞。
        """
        return any(count >= limit for count in self._tool_call_hashes.values())

    def get_stagnation_detail(self, limit: int = 3) -> str:
        """获取停滞的详细信息。"""
        for call_hash, count in self._tool_call_hashes.items():
            if count >= limit:
                label = self._tool_call_labels.get(call_hash, "相同工具调用")
                return f"{label} 已重复 {count} 次"
        return ""

    @staticmethod
    def _hash_call(name: str, args: dict[str, Any]) -> str:
        """生成工具调用的 hash（用于去重检测）。"""
        raw = f"{name}:{json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)}"
        return hashlib.md5(raw.encode()).hexdigest()[:12]

    @staticmethod
    def _label_call(name: str, args: dict[str, Any]) -> str:
        """Human-readable label for repeated-call diagnostics."""
        try:
            raw_args = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            raw_args = str(args)
        if len(raw_args) > 160:
            raw_args = f"{raw_args[:157]}..."
        return f"{name}({raw_args})"
