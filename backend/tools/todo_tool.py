"""
TodoWrite 工具（参考 Claude Code TodoWriteTool）。

让 Agent 能够创建和管理结构化的任务清单，用于：
  - 跟踪复杂多步骤任务进度
  - 向用户展示当前工作状态
  - 确保不遗漏多个子任务
  - 帮助 Agent 自身组织工作流程

权限: AUTO（无副作用）
"""

from __future__ import annotations

import logging
from typing import Any

from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema

logger = logging.getLogger(__name__)

# Todo item 合法状态
VALID_STATUSES = {"pending", "in_progress", "completed"}
VALID_PRIORITIES = {"high", "medium", "low"}
MAX_TODO_ITEMS = 50


class TodoWriteTool(BaseTool):
    """
    创建和管理会话级任务清单。

    使用场景（参考 Claude Code TodoWriteTool.prompt）：
    - 复杂多步骤任务（≥ 3 步）
    - 用户提供多个待办事项
    - 需要跟踪进度的非平凡任务
    - 开始工作前先规划步骤

    不适用场景：
    - 单个简单任务
    - 纯对话/信息性问答
    - 3 步以内的简单操作
    """

    name = "todo_write"
    read_only = False  # 会修改会话状态
    description = (
        "创建或更新会话任务清单，用于模型自驱动地跟踪复杂工作进度。"
        "每个 todo 包含 id、content、status（pending/in_progress/completed）、priority（high/medium/low）。"
        "当任务包含多个子任务、多个文件、用户给出清单、需要阶段性验证，或可能委托 task 子 agent 时，"
        "先创建清单并在执行过程中持续更新。"
        "这不是系统编排计划；它只是当前 agent loop 的可见 checklist。"
        "简单单步问答或无需跟踪的操作不要使用。"
    )
    permission = PermissionLevel.AUTO

    def __init__(self) -> None:
        self._todos: dict[str, list[dict[str, str]]] = {}

    def get_session_todos(self, session_id: str = "default") -> list[dict[str, str]]:
        """获取指定会话的当前 todo 列表。"""
        return self._todos.get(session_id, [])

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "todos": {
                        "type": "array",
                        "description": (
                            "完整的更新后任务清单。每次调用必须传入完整列表（不是增量更新）。"
                            "每个 item 包含: id（唯一标识）, content（任务描述）, "
                            "status（pending/in_progress/completed）, priority（high/medium/low）。"
                            "同一时刻建议只有一个 item 为 in_progress。"
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {
                                    "type": "string",
                                    "description": "任务唯一标识符",
                                },
                                "content": {
                                    "type": "string",
                                    "description": "任务描述",
                                },
                                "status": {
                                    "type": "string",
                                    "enum": ["pending", "in_progress", "completed"],
                                    "description": "任务状态",
                                },
                                "priority": {
                                    "type": "string",
                                    "enum": ["high", "medium", "low"],
                                    "description": "任务优先级",
                                },
                            },
                            "required": ["id", "content", "status", "priority"],
                        },
                        "maxItems": MAX_TODO_ITEMS,
                    },
                },
                "required": ["todos"],
            },
        )

    async def execute(
        self,
        args: dict[str, Any],
        context: Any = None,
    ) -> ToolResult:
        todos = args.get("todos", [])
        if not isinstance(todos, list):
            return self._error_result("todos 必须是数组")

        if len(todos) > MAX_TODO_ITEMS:
            return self._error_result(f"任务数量不能超过 {MAX_TODO_ITEMS}")

        # 验证每个 todo item
        validated: list[dict[str, str]] = []
        for item in todos:
            if not isinstance(item, dict):
                return self._error_result(f"每个 todo item 必须是对象: {item}")

            item_id = str(item.get("id", "")).strip()
            content = str(item.get("content", "")).strip()
            status = str(item.get("status", "pending")).strip()
            priority = str(item.get("priority", "medium")).strip()

            if not item_id:
                return self._error_result("每个 todo item 必须有 id")
            if not content:
                return self._error_result(f"todo item '{item_id}' 缺少 content")
            if status not in VALID_STATUSES:
                return self._error_result(
                    f"无效状态 '{status}'，必须是: {', '.join(sorted(VALID_STATUSES))}"
                )
            if priority not in VALID_PRIORITIES:
                return self._error_result(
                    f"无效优先级 '{priority}'，必须是: {', '.join(sorted(VALID_PRIORITIES))}"
                )

            validated.append({
                "id": item_id,
                "content": content,
                "status": status,
                "priority": priority,
            })

        # 获取会话 ID
        session_id = "default"
        if context and hasattr(context, "session_id") and context.session_id:
            session_id = context.session_id

        # 保存旧状态（用于 diff）
        old_todos = self._todos.get(session_id, [])

        # 如果所有任务都完成了，清空列表
        all_done = all(t["status"] == "completed" for t in validated) if validated else False
        new_todos = [] if all_done else validated

        self._todos[session_id] = new_todos

        # 构建摘要
        summary = self._build_summary(old_todos, validated, all_done)
        return self._success_result(summary)

    def _build_summary(
        self,
        old_todos: list[dict[str, str]],
        new_todos: list[dict[str, str]],
        all_done: bool,
    ) -> str:
        """构建任务清单变更摘要。"""
        if all_done:
            return (
                f"所有 {len(new_todos)} 个任务已完成！任务清单已清空。"
                "请继续处理后续任务（如果有的话）。"
            )

        if not old_todos:
            # 新建清单
            lines = ["任务清单已创建："]
            for t in new_todos:
                status_icon = self._status_icon(t["status"])
                lines.append(f"  {status_icon} [{t['priority']}] {t['content']}")
            return "\n".join(lines)

        # 更新清单
        old_map = {t["id"]: t for t in old_todos}
        changes: list[str] = []
        for t in new_todos:
            old = old_map.get(t["id"])
            if not old:
                changes.append(f"  + 新增: {t['content']}")
            elif old["status"] != t["status"]:
                changes.append(
                    f"  → {t['content']}: {old['status']} → {t['status']}"
                )

        counts = {"pending": 0, "in_progress": 0, "completed": 0}
        for t in new_todos:
            counts[t["status"]] = counts.get(t["status"], 0) + 1

        summary_parts = [
            "任务清单已更新。",
            f"状态: {counts['completed']} 完成, {counts['in_progress']} 进行中, {counts['pending']} 待处理",
        ]
        if changes:
            summary_parts.append("变更:\n" + "\n".join(changes))

        return "\n".join(summary_parts)

    @staticmethod
    def _status_icon(status: str) -> str:
        return {"pending": "○", "in_progress": "◐", "completed": "●"}.get(status, "?")
