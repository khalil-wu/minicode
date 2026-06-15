"""
记忆操作工具（DESIGN.md §2.2 + §4.3 + §8.2）。

让 Agent 可以读写文件记忆和向量记忆系统。

工具：
  read_memory(filename)   — 读取具体记忆文件
  save_memory(filename, content) — 写入/更新记忆文件
  recall_memory(query)    — 语义检索向量记忆
  remember_memory(content) — 写入长期向量记忆
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from backend.memory.file_memory import FileMemory
from backend.memory.vector_memory import VectorMemory
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema

if TYPE_CHECKING:
    from backend.permissions.context import ToolExecutionContext


class ReadMemoryTool(BaseTool):
    """读取记忆文件。"""

    name = "read_memory"
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
    mutates_external_state = True
    description = (
        "Write or update a persistent memory file for cross-session recall. "
        "Use this when the user provides explicit preferences (coding style, language, frameworks), "
        "project context (goals, tech stack, constraints), or corrections to your behavior. "
        "Files persist across sessions. Use sparingly — only save what the user explicitly states or corrects."
    )
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

    async def execute(self, args: dict[str, Any], context: ToolExecutionContext | None = None) -> ToolResult:
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


class RecallMemoryTool(BaseTool):
    """
    语义检索向量记忆。

    Agent 用自然语言查询，返回最相关的记忆摘要列表。
    权限: AUTO
    """

    name = "recall_memory"
    read_only = True
    description = (
        "Semantically search long-term vector memory for relevant past knowledge.\n\n"
        "WHEN TO USE:\n"
        "- Recalling fuzzy/conceptual knowledge ('what did the user prefer for testing?')\n"
        "- When semantic similarity matters more than exact keywords\n"
        "- At the start of a task, to check if relevant context was stored in past sessions\n\n"
        "WHEN NOT TO USE:\n"
        "- Reading a known structured config file — use read_memory instead\n"
        "- Searching workspace code — use grep_files instead\n\n"
        "Returns ranked memory summaries with memory_id and relevance score. "
        "Example: recall_memory(query='user's preferences for TypeScript')."
    )
    permission = PermissionLevel.AUTO

    def __init__(self, vector_memory: VectorMemory) -> None:
        self._vector_memory = vector_memory

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "required": ["query"],
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "自然语言查询，描述你想检索的记忆内容",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "返回结果数量上限，默认 5",
                    },
                },
            },
        )

    async def execute(self, args: dict[str, Any], context: ToolExecutionContext | None = None) -> ToolResult:
        query = args.get("query", "")
        top_k = args.get("top_k", 5)

        if not query:
            return self._error_result("缺少 query 参数")

        results = self._vector_memory.recall(query=query, top_k=top_k)

        if not results:
            return self._success_result("未找到相关记忆。")

        lines = [f"找到 {len(results)} 条相关记忆:"]
        for item in results:
            lines.append(
                f"  [{item['memory_id']}] (相关度 {item['score']:.2f}, "
                f"重要性 {item['importance']}) {item['summary']}"
            )
        return self._success_result("\n".join(lines))


class RememberMemoryTool(BaseTool):
    """
    写入长期向量记忆。

    Agent 将重要信息持久化到向量存储，后续可通过 recall_memory 检索。
    权限: AUTO（记忆写入风险低）
    """

    name = "remember_memory"
    mutates_external_state = True
    description = (
        "Save important cross-session knowledge to long-term vector memory.\n\n"
        "WHEN TO USE:\n"
        "- User explicitly states a preference or decision ('I prefer pytest over unittest')\n"
        "- Project-level architectural decisions that should persist\n"
        "- Important findings or conclusions that future sessions should know\n\n"
        "WHEN NOT TO USE:\n"
        "- Structured config data — use save_memory to write a file instead (e.g. user_profile.md)\n"
        "- Temporary facts only relevant to this session\n"
        "- Information already captured in workspace files (code, docs, git history)\n\n"
        "Returns memory_id for reference. "
        "Example: remember_memory(content='User prefers pytest for testing', tags=['preference','testing'])."
    )
    permission = PermissionLevel.AUTO

    def __init__(self, vector_memory: VectorMemory) -> None:
        self._vector_memory = vector_memory

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "required": ["content"],
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "要记住的内容，应简洁明确",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "标签列表，用于分类和过滤，如 ['preference', 'testing']",
                    },
                    "importance": {
                        "type": "integer",
                        "description": "重要性 1-5，默认 3。5=关键决策，1=参考信息",
                    },
                },
            },
        )

    async def execute(self, args: dict[str, Any], context: ToolExecutionContext | None = None) -> ToolResult:
        content = args.get("content", "")
        tags = args.get("tags", [])
        importance = args.get("importance", 3)

        if not content:
            return self._error_result("缺少 content 参数")

        metadata = {}
        if context:
            if getattr(context, "session_id", None):
                metadata["session_id"] = context.session_id
            if getattr(context, "task_id", None):
                metadata["task_id"] = context.task_id
            if getattr(context, "permission", None):
                metadata["source"] = getattr(context.permission, "source", None)
                metadata["permission_mode"] = getattr(context.permission, "mode", None)

        memory_id = self._vector_memory.remember(
            content=content,
            tags=tags,
            importance=importance,
            metadata=metadata,
        )
        return self._success_result(
            content=f"已存入长期记忆 (id={memory_id}, 重要性={importance})"
        )
