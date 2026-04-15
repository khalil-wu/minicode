"""
搜索工具（DESIGN.md §8.2）。

  - grep_files: 正则搜索文件内容。≤50 条匹配行。权限: AUTO
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema

GREP_MAX_MATCHES = 50


class GrepFilesTool(BaseTool):
    """
    在目录中搜索匹配正则表达式的文件内容。

    返回匹配行的文件路径、行号和内容。最多 50 条结果。
    权限: AUTO
    """

    name = "grep_files"
    description = (
        "在指定目录中搜索匹配正则表达式模式的文件内容。"
        "返回匹配行列表，包含文件路径、行号和内容。最多返回 50 条结果。"
        "示例: grep_files(pattern='def run_agent', directory='./backend')。"
        "注意: 自动跳过二进制文件和隐藏目录。"
    )
    permission = PermissionLevel.AUTO

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "正则表达式搜索模式",
                    },
                    "directory": {
                        "type": "string",
                        "description": "搜索目录路径，默认当前目录",
                    },
                    "file_extensions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "限定文件扩展名列表，如 ['.py', '.js']，为空则搜索所有文本文件",
                    },
                    "case_insensitive": {
                        "type": "boolean",
                        "description": "是否忽略大小写，默认 false",
                    },
                },
                "required": ["pattern"],
            },
        )

    async def execute(self, args: dict[str, Any]) -> ToolResult:
        pattern = args.get("pattern", "")
        directory = args.get("directory", ".")
        file_extensions = args.get("file_extensions", [])
        case_insensitive = args.get("case_insensitive", False)

        if not pattern:
            return self._error_result("缺少 pattern 参数")

        path = Path(directory)
        if not path.exists():
            return self._error_result(f"目录不存在: {directory}")

        try:
            flags = re.IGNORECASE if case_insensitive else 0
            regex = re.compile(pattern, flags)
        except re.error as exc:
            return self._error_result(f"无效的正则表达式: {exc}")

        matches: list[str] = []
        files_searched = 0

        for file_path in sorted(path.rglob("*")):
            if not file_path.is_file():
                continue

            # 跳过隐藏文件、__pycache__、.git
            parts = file_path.relative_to(path).parts
            if any(
                p.startswith(".") or p == "__pycache__" or p == "node_modules"
                for p in parts
            ):
                continue

            # 扩展名过滤
            if file_extensions and file_path.suffix not in file_extensions:
                continue

            try:
                content = file_path.read_text(encoding="utf-8", errors="ignore")
            except (PermissionError, OSError):
                continue

            files_searched += 1

            for line_num, line in enumerate(content.split("\n"), 1):
                if regex.search(line):
                    rel_path = file_path.relative_to(path) if path != file_path else file_path
                    matches.append(f"  {rel_path}:{line_num}: {line.rstrip()}")
                    if len(matches) >= GREP_MAX_MATCHES:
                        break

            if len(matches) >= GREP_MAX_MATCHES:
                break

        if not matches:
            return self._success_result(
                f"在 {directory} 中搜索 '{pattern}'：无匹配结果（搜索了 {files_searched} 个文件）"
            )

        header = f"在 {directory} 中搜索 '{pattern}'：找到 {len(matches)} 处匹配"
        if len(matches) >= GREP_MAX_MATCHES:
            header += f"（已截断，上限 {GREP_MAX_MATCHES} 条）"

        result = header + "\n\n" + "\n".join(matches)
        return self._success_result(result)
