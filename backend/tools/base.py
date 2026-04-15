"""
工具系统基础抽象。

设计原则（来自 DESIGN.md §6.4 + design_principle.md §三-(3)）：
  - Token-efficient: 默认返回摘要，完整内容存 artifact
  - Non-overlapping: 工具间功能互斥
  - Self-contained: 名称自描述
  - Robust: 异常返回自然语言提示，不返回 stack trace
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class PermissionLevel(Enum):
    """工具调用权限级别（DESIGN.md §8.3）。"""

    AUTO = "auto"  # 自动执行
    CONFIRM = "confirm"  # 展示参数，用户确认
    DIFF_REVIEW = "diff"  # 展示 diff，用户审批
    ALWAYS_DENY = "deny"  # 永远拒绝


@dataclass
class ToolResult:
    """
    工具执行结果（DESIGN.md §3.4）。

    消息层/产物层分离的核心数据结构：
    - content: 短摘要（≤ 500 tokens），始终注入 context
    - artifact_id: 内容过长时存 artifact，给 Agent 引用
    - artifact_preview: 前几行预览
    - is_error: 是否为错误结果
    """

    content: str
    artifact_id: str | None = None
    artifact_preview: str | None = None
    is_error: bool = False

    def to_context_string(self) -> str:
        """生成注入 context 的压缩表示。"""
        parts = [self.content]
        if self.artifact_preview:
            parts.append(f"预览:\n{self.artifact_preview}")
        if self.artifact_id:
            parts.append(
                f"完整结果已存储，可用 read_artifact('{self.artifact_id}') 获取详情"
            )
        return "\n".join(parts)


@dataclass
class ToolSchema:
    """
    工具的 JSON Schema 定义（供 LLM function-calling 使用）。

    遵循 ACI 设计原则（DESIGN.md §14.2）：
    - 名称自描述
    - 参数无歧义
    - 描述含示例
    - 边界情况说明
    """

    name: str
    description: str
    parameters: dict[str, Any]
    strict: bool = False

    def to_openai_tool(self) -> dict[str, Any]:
        """转换为 OpenAI function-calling 格式。"""
        tool: dict[str, Any] = {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }
        if self.strict:
            tool["function"]["strict"] = True
        return tool

    def to_summary(self) -> str:
        """生成精简的一行描述（用于 token 预算不足时）。"""
        return f"- {self.name}: {self.description.split('.')[0]}"


class BaseTool(ABC):
    """
    工具基类。

    每个工具必须实现：
    - name: 自描述名称（read_file ✓, process_data ✗）
    - description: 含示例和边界说明的详细描述
    - input_schema: JSON Schema 参数定义
    - permission: 权限级别
    - execute(): 执行逻辑
    """

    name: str
    description: str
    permission: PermissionLevel = PermissionLevel.AUTO

    @abstractmethod
    def get_schema(self) -> ToolSchema:
        """返回该工具的 JSON Schema。"""

    @abstractmethod
    async def execute(self, args: dict[str, Any]) -> ToolResult:
        """
        执行工具。

        实现时必须：
        1. 在内部捕获异常
        2. 异常时返回 ToolResult(content=人类可读错误, is_error=True)
        3. 大输出存 artifact，只在 content 中放摘要
        """

    def _error_result(self, message: str) -> ToolResult:
        """生成标准化错误结果。"""
        return ToolResult(content=f"错误: {message}", is_error=True)

    def _success_result(
        self,
        content: str,
        artifact_id: str | None = None,
        artifact_preview: str | None = None,
    ) -> ToolResult:
        """生成标准化成功结果。"""
        return ToolResult(
            content=content,
            artifact_id=artifact_id,
            artifact_preview=artifact_preview,
        )
