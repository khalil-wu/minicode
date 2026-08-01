"""
记忆操作工具（DESIGN.md §2.2 + §4.3 + §8.2）。

让 Agent 可以读写文件记忆。

工具：
  read_memory(filename)   — 读取具体记忆文件
  save_memory(filename, content) — 写入/更新记忆文件

The legacy vector-memory tools below are retained only for explicit opt-in
compatibility. The default registry does not expose them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from backend.memory.file_memory import FileMemory
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema

if TYPE_CHECKING:
    from backend.permissions.context import ToolExecutionContext


class ReadMemoryTool(BaseTool):
    """读取记忆文件。"""

    name = "read_memory"
    result_kind = "memory"
    activity_kind = "fileRead"
    display_label = "Read memory"
    read_only = True
    description = (
        "Read a persistent memory file that stores user preferences, project context, or feedback across sessions. "
        "Available files: user_profile.md (user preferences, language, style), "
        "project_context.md (project goals, tech stack, constraints), "
        "feedback.md (corrections and confirmed approaches), reference.md (external resources). "
        "Use this at the start of a session to recall context, or when the user references prior work."
    )
    permission = PermissionLevel.AUTO

    def __init__(self, memory: FileMemory) -> None:
        self._memory = memory

    def _memory_for(self, context: ToolExecutionContext | None) -> FileMemory:
        workspace_root = getattr(context, "workspace_root", None) if context else None
        return FileMemory.for_workspace(workspace_root) if workspace_root else self._memory

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "required": ["filename"],
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": (
                            "记忆文件名（如 user_profile.md、project_context.md、"
                            "feedback.md、reference.md）"
                        ),
                    },
                },
            },
        )
    async def execute(self, args: dict[str, Any], context: ToolExecutionContext | None = None) -> ToolResult:
        filename = args.get("filename", "")
        if not filename:
            return self._error_result("缺少 filename 参数")

        memory = self._memory_for(context)
        content = memory.read_file(filename)
        if content is None:
            available = ", ".join(memory.list_files())
            return self._error_result(
                f"记忆文件 '{filename}' 不存在。可用文件: {available}"
            )

        return self._success_result(content=content)


class SaveMemoryTool(BaseTool):
    """写入/更新记忆文件。"""

    name = "save_memory"
    result_kind = "memory"
    activity_kind = "fileChange"
    display_label = "Save memory"
    mutates_external_state = True
    description = (
        "Write or update a persistent memory file for cross-session recall. "
        "Use this when the user provides explicit preferences (coding style, language, frameworks), "
        "project context (goals, tech stack, constraints), or corrections to your behavior. "
        "Files persist across sessions. Use sparingly — only save what the user explicitly states or corrects.\n\n"
        "RULES:\n"
        "- Do NOT store what the live workspace already records: code structure, file paths, "
        "current function signatures, git history, past fixes — these are derivable and go stale.\n"
        "- Convert relative dates to absolute (\"next Thursday\" → the actual date) so the memory "
        "stays meaningful later.\n"
        "- Keep it to the four kinds of durable memory: user profile, project context, "
        "behavior feedback, and external references."
    )
    permission = PermissionLevel.CONFIRM  # 写操作需确认

    def __init__(self, memory: FileMemory) -> None:
        self._memory = memory

    def _memory_for(self, context: ToolExecutionContext | None) -> FileMemory:
        workspace_root = getattr(context, "workspace_root", None) if context else None
        return FileMemory.for_workspace(workspace_root) if workspace_root else self._memory

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "required": ["filename", "content"],
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "记忆文件名（如 user_profile.md）",
                    },
                    "content": {
                        "type": "string",
                        "description": "要写入的完整内容",
                    },
                    "description": {
                        "type": "string",
                        "description": "简短描述（更新 MEMORY.md 索引用，≤80 字符）",
                    },
                },
            },
        )
    async def execute(self, args: dict[str, Any], context: ToolExecutionContext | None = None) -> ToolResult:
        filename = args.get("filename", "")
        content = args.get("content", "")
        description = args.get("description", "")

        if not filename or not content:
            return self._error_result("缺少 filename 或 content 参数")

        memory = self._memory_for(context)
        success = memory.save_file(filename, content)
        if not success:
            return self._error_result(f"写入 '{filename}' 失败")

        # 更新索引描述
        if description:
            memory.update_index_entry(filename, description)

        return self._success_result(
            content=f"已更新记忆文件 '{filename}'（{len(content)} 字符）"
        )
