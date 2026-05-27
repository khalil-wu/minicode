"""
Glob 工具 - 文件模式匹配（参考 Claude Code GlobTool）。

支持 glob 模式：
  - **/*.py      — 递归匹配所有 Python 文件
  - src/**/*.ts  — src 下所有 TypeScript 文件
  - *.json       — 当前目录 JSON 文件
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema

logger = logging.getLogger(__name__)


class GlobTool(BaseTool):
    """
    文件模式匹配工具。

    使用 glob 模式快速查找文件，支持递归搜索。
    权限: AUTO（只读操作）
    """

    name = "glob"
    description = (
        "使用 glob 模式匹配文件路径。"
        "支持 ** 递归匹配、* 通配符、? 单字符匹配。"
        "示例: glob(pattern='**/*.py') 查找所有 Python 文件。"
        "示例: glob(pattern='src/**/*.ts', path='./frontend') 在 frontend/src 下查找 TS 文件。"
    )
    permission = PermissionLevel.AUTO

    def __init__(self, workspace_root: Path | None = None):
        self._workspace_root = workspace_root or Path.cwd()

    def _resolve_workspace_root(self, context: Any = None) -> Path:
        if context and getattr(context, "workspace_root", None):
            return Path(context.workspace_root).resolve()
        return Path(self._workspace_root).resolve()

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Glob 模式，如 '**/*.py' 或 'src/**/*.ts'",
                    },
                    "path": {
                        "type": "string",
                        "description": "搜索起始目录（可选，默认工作区根目录）",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "最大返回文件数（默认 100）",
                        "default": 100,
                    },
                },
                "required": ["pattern"],
            },
        )

    async def execute(self, args: dict[str, Any], context: Any = None) -> ToolResult:
        pattern = args.get("pattern", "")
        path_str = args.get("path", ".")
        limit = args.get("limit", 100)

        if not pattern:
            return self._error_result("缺少 pattern 参数")

        workspace_root = self._resolve_workspace_root(context)
        path = Path(path_str)
        search_root = path.resolve() if path.is_absolute() else (workspace_root / path).resolve()
        try:
            search_root.relative_to(workspace_root)
        except ValueError:
            return self._error_result(f"路径超出工作区: {path_str}")
        if not search_root.exists():
            return self._error_result(f"路径不存在: {path_str}")

        if not search_root.is_dir():
            return self._error_result(f"不是目录: {path_str}")

        try:
            # 执行 glob 匹配
            matches = list(search_root.glob(pattern))

            # 过滤只保留文件
            files = [m for m in matches if m.is_file()]

            # 排序并限制数量
            files = sorted(files)[:limit]

            # 转换为相对路径
            relative_paths = [
                str(f.relative_to(workspace_root)) for f in files
            ]

            result_text = "\n".join(relative_paths) if relative_paths else "(无匹配文件)"

            return self._success_result(result_text)

        except Exception as e:
            logger.error(f"Glob 执行失败: {e}", exc_info=True)
            return self._error_result(f"Glob 执行失败: {e}")
