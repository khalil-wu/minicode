"""
模糊文件搜索工具（参考 Claude Code 的 fuzzy search 功能）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema
from backend.workspace.fuzzy_search import get_global_fuzzy_search


class FuzzySearchTool(BaseTool):
    """
    模糊文件搜索工具。

    使用智能评分算法查找文件：
    - 支持部分匹配（不需要完整文件名）
    - 边界匹配优先（路径分隔符、CamelCase）
    - 连续字符匹配加分
    - 自动排除测试文件（可选）
    """

    name = "fuzzy_search"
    read_only = True
    description = (
        "使用模糊匹配查找文件。"
        "支持部分文件名、路径片段、CamelCase 缩写等。"
        "示例: fuzzy_search(query='MainApp') 可以找到 'src/components/MainApp.tsx'。"
        "示例: fuzzy_search(query='utils/str') 可以找到 'backend/utils/string_helper.py'。"
    )
    permission = PermissionLevel.AUTO

    def __init__(self, workspace_root: Path):
        """
        初始化工具。

        Args:
            workspace_root: 工作区根目录
        """
        self.workspace_root = workspace_root

    def _resolve_workspace_root(self, context: ToolExecutionContext | None = None) -> Path:
        if context and getattr(context, "workspace_root", None):
            return Path(context.workspace_root).resolve()
        return Path(self.workspace_root).resolve()

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "搜索查询（文件名、路径片段、CamelCase 缩写等）",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "最大结果数（默认 20）",
                        "default": 20,
                    },
                    "include_tests": {
                        "type": "boolean",
                        "description": "是否包含测试文件（默认 false）",
                        "default": False,
                    },
                },
                "required": ["query"],
            },
            strict=True,
        )

    async def execute(
        self, args: dict[str, Any], context: ToolExecutionContext | None = None
    ) -> ToolResult:
        query = args.get("query", "")
        max_results = args.get("max_results", 20)
        include_tests = args.get("include_tests", False)

        if not query:
            return self._error_result("缺少 query 参数")

        workspace_root = self._resolve_workspace_root(context)

        # 获取搜索引擎
        engine = get_global_fuzzy_search(workspace_root)

        # 执行搜索
        matches = engine.search(
            query=query,
            max_results=max_results,
            include_tests=include_tests,
        )

        if not matches:
            return self._success_result(f"未找到匹配 '{query}' 的文件")

        # 格式化结果
        lines = [f"找到 {len(matches)} 个匹配 '{query}' 的文件:\n"]

        for i, match in enumerate(matches, 1):
            rel_path = match.path.relative_to(workspace_root)
            lines.append(f"{i}. {rel_path} (score: {match.score:.1f})")

        result = "\n".join(lines)
        return self._success_result(result)
