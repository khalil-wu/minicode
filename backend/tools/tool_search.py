"""
ToolSearch 工具（参考 Claude Code ToolSearchTool 延迟加载模式）。

当注册的工具数量较多时，避免将所有工具 schema 一次性发给 LLM（会消耗大量 tokens）。
ToolSearchTool 让 Agent 能够按关键词搜索可用工具，按需发现和使用。

使用场景：
  - Agent 不确定用什么工具时
  - 工具数量 > 15 时自动启用
  - 减少 tool_schemas 的 token 消耗

权限: AUTO
"""

from __future__ import annotations

import logging
from typing import Any

from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema

logger = logging.getLogger(__name__)


class ToolSearchTool(BaseTool):
    """
    搜索可用工具。

    当 Agent 不确定要用什么工具时，可以通过关键词搜索
    发现合适的工具。返回匹配的工具名称和描述。
    """

    name = "tool_search"
    read_only = True
    description = (
        "搜索可用工具。当你不确定该用什么工具来完成任务时，"
        "用关键词搜索来发现合适的工具。"
        "示例: tool_search(query='git') 查找 Git 相关工具。"
        "示例: tool_search(query='search file') 查找文件搜索工具。"
    )
    permission = PermissionLevel.AUTO

    def __init__(self) -> None:
        self._tool_index: list[dict[str, str]] = []

    def update_index(self, tools: list[BaseTool]) -> None:
        """从当前注册的工具列表构建搜索索引。"""
        self._tool_index = []
        for tool in tools:
            if tool.name == self.name:
                continue
            schema = tool.get_schema()
            self._tool_index.append({
                "name": schema.name,
                "description": schema.description,
                "permission": tool.permission.value,
                "read_only": str(tool.read_only),
            })

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "搜索关键词，用于匹配工具名称和描述",
                    },
                },
                "required": ["query"],
            },
        )

    async def execute(
        self,
        args: dict[str, Any],
        context: Any = None,
    ) -> ToolResult:
        query = str(args.get("query", "")).strip().lower()
        if not query:
            return self._error_result("请提供搜索关键词")

        # 简单的关键词匹配
        matches: list[dict[str, str]] = []
        keywords = query.split()

        for tool_info in self._tool_index:
            name_lower = tool_info["name"].lower()
            desc_lower = tool_info["description"].lower()
            searchable = f"{name_lower} {desc_lower}"

            # 所有关键词都匹配
            if all(kw in searchable for kw in keywords):
                matches.append(tool_info)

        if not matches:
            # 退化到单关键词部分匹配
            for tool_info in self._tool_index:
                name_lower = tool_info["name"].lower()
                desc_lower = tool_info["description"].lower()
                searchable = f"{name_lower} {desc_lower}"
                if any(kw in searchable for kw in keywords):
                    matches.append(tool_info)

        if not matches:
            return self._success_result(
                f"没有找到匹配 '{query}' 的工具。"
                f"当前共有 {len(self._tool_index)} 个可用工具。"
            )

        lines = [f"找到 {len(matches)} 个匹配的工具：\n"]
        for m in matches[:10]:
            ro = " [只读]" if m["read_only"] == "True" else ""
            perm = f" [权限:{m['permission']}]"
            lines.append(f"- **{m['name']}**{ro}{perm}")
            # 截取描述前80字符
            desc = m["description"][:80]
            if len(m["description"]) > 80:
                desc += "..."
            lines.append(f"  {desc}")

        return self._success_result("\n".join(lines))
