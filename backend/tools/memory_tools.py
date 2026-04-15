"""
记忆操作工具（DESIGN.md §2.2 + §8.2）。

让 Agent 可以读写文件记忆系统。

工具：
  read_memory(filename) — 读取具体记忆文件
  save_memory(filename, content) — 写入/更新记忆文件
"""

from __future__ import annotations

from typing import Any

from backend.memory.file_memory import FileMemory
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema


class ReadMemoryTool(BaseTool):
    """读取记忆文件。"""

    name = "read_memory"
    description = "读取记忆文件的内容。可用的记忆文件包括用户偏好、项目背景、反馈记录等。"
    permission = PermissionLevel.AUTO

    def __init__(self, memory: FileMemory) -> None:
        self._memory = memory

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

    async def execute(self, args: dict[str, Any]) -> ToolResult:
        filename = args.get("filename", "")
        if not filename:
            return self._error_result("缺少 filename 参数")

        content = self._memory.read_file(filename)
        if content is None:
            available = ", ".join(self._memory.list_files())
            return self._error_result(
                f"记忆文件 '{filename}' 不存在。可用文件: {available}"
            )

        return self._success_result(content=content)


class SaveMemoryTool(BaseTool):
    """写入/更新记忆文件。"""

    name = "save_memory"
    description = "写入或更新记忆文件。用于保存用户偏好、项目背景、反馈等持久信息。"
    permission = PermissionLevel.CONFIRM  # 写操作需确认

    def __init__(self, memory: FileMemory) -> None:
        self._memory = memory

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

    async def execute(self, args: dict[str, Any]) -> ToolResult:
        filename = args.get("filename", "")
        content = args.get("content", "")
        description = args.get("description", "")

        if not filename or not content:
            return self._error_result("缺少 filename 或 content 参数")

        success = self._memory.save_file(filename, content)
        if not success:
            return self._error_result(f"写入 '{filename}' 失败")

        # 更新索引描述
        if description:
            self._memory.update_index_entry(filename, description)

        return self._success_result(
            content=f"已更新记忆文件 '{filename}'（{len(content)} 字符）"
        )
