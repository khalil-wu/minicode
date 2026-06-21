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

import json
import logging
from pathlib import Path
from typing import Any

from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema

logger = logging.getLogger(__name__)

# Todo item 合法状态
VALID_STATUSES = {"pending", "in_progress", "completed"}
VALID_PRIORITIES = {"high", "medium", "low"}
MAX_TODO_ITEMS = 50
STATUS_ICONS = {"pending": "○", "in_progress": "◐", "completed": "●"}


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
    # Mutates internal session state, not user workspace files. Keep
    # read_only=False so it is still serialized, but do not project it as a
    # workspace/file mutation.
    mutates_workspace = False
    read_only = False  # 会修改会话状态
    description = (
        "Create or update the current session todo checklist. Use this proactively and often for "
        "complex coding work so progress is visible and requirements are not lost.\n\n"
        "When to use: complex multi-step work (3+ meaningful steps), multi-file changes, a user-provided "
        "list of tasks, non-trivial investigation, tasks that need tests/build verification, or newly discovered "
        "follow-up work. Call todo_write before the first substantive work/tool call, with exactly one item "
        "already in_progress.\n\n"
        "When not to use: a single straightforward edit, a trivial command, pure conversation, or an informational "
        "answer with no work to track.\n\n"
        "State rules: pass the complete updated list every time; keep at most one item in_progress; mark a task "
        "completed immediately after it is fully done; do not batch several completions into one late update. "
        "Never mark completed while tests fail, verification is unfinished, implementation is partial, or a blocker "
        "remains. Remove obsolete tasks entirely instead of keeping stale pending items.\n\n"
        "Each item needs both content (imperative, e.g. 'Run tests') and activeForm (present continuous, e.g. "
        "'Running tests'). This is a live checklist, not the larger visible update_plan."
    )
    permission = PermissionLevel.AUTO

    def __init__(self, workspace_root: Path | None = None) -> None:
        self._todos: dict[str, list[dict[str, str]]] = {}
        self._workspace_root = workspace_root

    def _persist_path(self, session_id: str) -> Path | None:
        if not self._workspace_root:
            return None
        return self._workspace_root / ".minicode" / "todos" / f"{session_id}.json"

    def _load_from_disk(self, session_id: str) -> list[dict[str, str]]:
        path = self._persist_path(session_id)
        if path and path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return []

    def _save_to_disk(self, session_id: str, todos: list[dict[str, str]]) -> None:
        path = self._persist_path(session_id)
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(todos, ensure_ascii=False, indent=2), encoding="utf-8")

    def get_session_todos(self, session_id: str = "default") -> list[dict[str, str]]:
        if session_id not in self._todos:
            self._todos[session_id] = self._load_from_disk(session_id)
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
                            "Complete updated todo list, not a patch or incremental delta. "
                            "For active work, include exactly one in_progress item and mark completed items "
                            "immediately when they are fully done. "
                            "Each item: id, content (imperative, e.g. 'Run tests'), "
                            "activeForm (present continuous, e.g. 'Running tests'), "
                            "status (pending/in_progress/completed), priority (high/medium/low). "
                            "Never mark completed if tests fail, verification is unfinished, work is partial, "
                            "or a blocker remains."
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
                                    "description": "任务描述（祈使形式，如'修复认证 bug'）",
                                },
                                "activeForm": {
                                    "type": "string",
                                    "description": "进行中时显示的描述（进行时形式，如'正在修复认证 bug'）",
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
                            "required": ["id", "content", "activeForm", "status", "priority"],
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

        logger.info(f"[TODO] todo_write called with {len(todos)} items")

        # 验证每个 todo item
        validated: list[dict[str, str]] = []
        for item in todos:
            if not isinstance(item, dict):
                return self._error_result(f"每个 todo item 必须是对象: {item}")

            item_id = str(item.get("id", "")).strip()
            content = str(item.get("content", "")).strip()
            status = str(item.get("status", "pending")).strip()
            priority = str(item.get("priority", "medium")).strip()
            active_form = str(item.get("activeForm", "")).strip()

            if not item_id:
                return self._error_result("每个 todo item 必须有 id")
            if not content:
                return self._error_result(f"todo item '{item_id}' 缺少 content")
            if not active_form:
                return self._error_result(
                    f"todo item '{item_id}' 缺少 activeForm；"
                    "请提供进行中显示文案，例如 '正在运行测试'"
                )
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
                "activeForm": active_form,
            })

        # 获取会话 ID
        in_progress_count = sum(1 for item in validated if item["status"] == "in_progress")
        if in_progress_count > 1:
            return self._error_result("同一时刻最多只能有一个 in_progress todo item")

        # 获取会话 ID
        session_id = "default"
        if context and hasattr(context, "session_id") and context.session_id:
            session_id = context.session_id

        # 保存旧状态（用于 diff）
        old_todos = self._todos.get(session_id, [])

        # Claude Code semantics: when every item is completed, the visible app
        # checklist is cleared. The model still receives the completed list in
        # the summary below, but session state/disk should not keep stale done
        # items around.
        all_done = all(t["status"] == "completed" for t in validated) if validated else False
        new_todos = [] if all_done else validated

        self._todos[session_id] = new_todos
        self._save_to_disk(session_id, new_todos)

        logger.info(f"[TODO] Session {session_id}: {len(new_todos)} tasks saved")
        for t in new_todos:
            logger.info(f"[TODO]   #{t['id']}: {t['content']} [{t['status']}]")

        if context and context.emit_event:
            try:
                for todo in validated:
                    await context.emit_event("task.update", {
                        "todo_id": todo["id"],
                        "status": todo["status"],
                        "content": todo["content"],
                        "activeForm": todo.get("activeForm", ""),
                    })
                if all_done:
                    await context.emit_event("task.update", {"todos": []})
                logger.info(
                    "[TODO] Emitted %d task.update events via WebSocket%s",
                    len(validated),
                    " + clear snapshot" if all_done else "",
                )
            except Exception as e:
                logger.warning(f"[TODO] Failed to emit task events: {e}")

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
        return STATUS_ICONS.get(status, "?")


class TodoReadTool(BaseTool):
    """读取当前会话的任务清单。"""

    name = "todo_read"
    read_only = True
    description = (
        "Read the current session's task checklist without modifying it. "
        "Use this to check what tasks are pending, in progress, or completed before deciding what to do next. "
        "Useful after returning from a subagent or after a long tool execution to re-orient. "
        "Returns a formatted list with status icons and priorities."
    )
    permission = PermissionLevel.AUTO

    def __init__(self, todo_write_tool: TodoWriteTool | None = None) -> None:
        self._todo_tool = todo_write_tool

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "status_filter": {
                        "type": "string",
                        "enum": ["pending", "in_progress", "completed", "all"],
                        "description": "Filter by status. Default: 'all'",
                    },
                },
            },
        )

    async def execute(
        self,
        args: dict[str, Any],
        context: Any = None,
    ) -> ToolResult:
        if not self._todo_tool:
            return self._error_result("No todo_write tool available")

        session_id = "default"
        if context and hasattr(context, "session_id") and context.session_id:
            session_id = context.session_id

        todos = self._todo_tool.get_session_todos(session_id)
        status_filter = args.get("status_filter", "all")

        if status_filter != "all":
            todos = [t for t in todos if t.get("status") == status_filter]

        if not todos:
            if status_filter == "all":
                return self._success_result("No tasks in the current session. Use todo_write to create a task list.")
            return self._success_result(f"No tasks with status '{status_filter}'.")

        lines = [f"Current tasks ({len(todos)}):"]
        for t in todos:
            icon = STATUS_ICONS.get(t.get("status", ""), "?")
            pri = t.get("priority", "medium")
            lines.append(f"  {icon} [{pri}] {t['content']}")

        counts = {"pending": 0, "in_progress": 0, "completed": 0}
        for t in self._todo_tool.get_session_todos(session_id):
            counts[t.get("status", "")] = counts.get(t.get("status", ""), 0) + 1
        lines.append(f"\nOverall: {counts['completed']} done, {counts['in_progress']} in progress, {counts['pending']} pending")

        return self._success_result("\n".join(lines))
