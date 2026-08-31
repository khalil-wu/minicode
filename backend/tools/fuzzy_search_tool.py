"""MiniCode fuzzy file search tool."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema
from backend.workspace.fuzzy_search import get_global_fuzzy_search

if TYPE_CHECKING:
    from backend.permissions.context import ToolExecutionContext


_WORKSPACE_ROOT_UNSET = object()


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
    result_kind = "file"
    activity_kind = "workspaceSearch"
    display_label = "Find files"
    read_only = True
    description = (
        "Find files by partial name match using fuzzy scoring. "
        "Supports partial filenames, path segments, CamelCase abbreviations.\n\n"
        "WHEN TO USE:\n"
        "- You know roughly what the file is called but not the exact name or path\n"
        "- Searching for files by feature/component name (e.g. 'UserAuth' finds 'src/auth/UserAuthentication.tsx')\n"
        "- Finding files across deep directory trees without knowing the full path\n\n"
        "WHEN NOT TO USE:\n"
        "- Finding files by content — use grep_files instead\n"
        "- Listing all files in a directory — use list_files instead\n"
        "- Pattern matching (*.py, **/*.test.ts) — use glob_files instead\n\n"
        "Examples:\n"
        "- fuzzy_search(query='MainApp') → finds 'src/components/MainApp.tsx'\n"
        "- fuzzy_search(query='utils/str') → finds 'backend/utils/string_helper.py'\n"
        "- fuzzy_search(query='UserSvc') → finds 'services/UserService.java'"
    )
    permission = PermissionLevel.AUTO

    def __init__(self, workspace_root: Path | None | object = _WORKSPACE_ROOT_UNSET):
        """
        初始化工具。

        Args:
            workspace_root: 工作区根目录
        """
        self.workspace_root = (
            Path.cwd()
            if workspace_root is _WORKSPACE_ROOT_UNSET
            else Path(workspace_root).expanduser().resolve()
            if workspace_root is not None
            else None
        )

    def _resolve_workspace_root(self, context: ToolExecutionContext | None = None) -> Path | None:
        if context and getattr(context, "workspace_root", None):
            return Path(context.workspace_root).resolve()
        return self.workspace_root.resolve() if self.workspace_root is not None else None

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
        if workspace_root is None:
            return self._error_result("Fuzzy search requires an open workspace.")

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
