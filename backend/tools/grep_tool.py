"""
Grep 工具 - 代码内容搜索（参考 Claude Code GrepTool）。

基于 ripgrep 的高性能代码搜索，支持：
  - 正则表达式
  - 文件类型过滤
  - 上下文行显示
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any

from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema

logger = logging.getLogger(__name__)


class GrepTool(BaseTool):
    """
    代码内容搜索工具（基于 ripgrep 或 Python 实现）。

    支持正则表达式、文件类型过滤、上下文行显示。
    权限: AUTO（只读操作）
    """

    name = "grep"
    description = (
        "在文件中搜索文本内容，支持正则表达式。"
        "示例: grep(pattern='def.*main', glob='**/*.py') 搜索所有 Python 文件中的 main 函数定义。"
        "示例: grep(pattern='TODO', context=2) 搜索 TODO 注释并显示上下文。"
    )
    permission = PermissionLevel.AUTO

    def __init__(self, workspace_root: Path | None = None):
        self._workspace_root = workspace_root or Path.cwd()
        self._has_ripgrep = self._check_ripgrep()

    def _resolve_workspace_root(self, context: Any = None) -> Path:
        if context and getattr(context, "workspace_root", None):
            return Path(context.workspace_root).resolve()
        return Path(self._workspace_root).resolve()

    def _check_ripgrep(self) -> bool:
        """检查是否安装了 ripgrep"""
        try:
            import shutil
            return shutil.which("rg") is not None
        except Exception:
            return False

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "搜索模式（支持正则表达式）",
                    },
                    "glob": {
                        "type": "string",
                        "description": "文件 glob 模式（如 '**/*.py'），可选",
                    },
                    "path": {
                        "type": "string",
                        "description": "搜索目录（可选，默认工作区根目录）",
                    },
                    "context": {
                        "type": "integer",
                        "description": "显示匹配行前后的上下文行数（默认 0）",
                        "default": 0,
                    },
                    "case_sensitive": {
                        "type": "boolean",
                        "description": "是否区分大小写（默认 true）",
                        "default": True,
                    },
                    "limit": {
                        "type": "integer",
                        "description": "最大返回结果数（默认 50）",
                        "default": 50,
                    },
                },
                "required": ["pattern"],
            },
        )

    async def execute(self, args: dict[str, Any], context: Any = None) -> ToolResult:
        pattern = args.get("pattern", "")
        glob_pattern = args.get("glob")
        path_str = args.get("path", ".")
        context_lines = args.get("context", 0)
        case_sensitive = args.get("case_sensitive", True)
        limit = args.get("limit", 50)

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

        try:
            if self._has_ripgrep:
                result = await self._grep_with_ripgrep(
                    pattern, search_root, glob_pattern, context_lines, case_sensitive, limit
                )
            else:
                result = await self._grep_python(
                    pattern, search_root, glob_pattern, context_lines, case_sensitive, limit, workspace_root
                )

            return result

        except Exception as e:
            logger.error(f"Grep 执行失败: {e}", exc_info=True)
            return self._error_result(f"Grep 执行失败: {e}")

    async def _grep_with_ripgrep(
        self,
        pattern: str,
        search_root: Path,
        glob_pattern: str | None,
        context_lines: int,
        case_sensitive: bool,
        limit: int,
    ) -> ToolResult:
        """使用 ripgrep 执行搜索"""
        cmd = ["rg", "--line-number", "--no-heading", "--color=never"]

        if not case_sensitive:
            cmd.append("--ignore-case")

        if context_lines > 0:
            cmd.extend(["-C", str(context_lines)])

        if glob_pattern:
            cmd.extend(["--glob", glob_pattern])

        cmd.extend(["--max-count", str(limit)])
        cmd.append(pattern)
        cmd.append(str(search_root))

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout, stderr = await proc.communicate()

        if proc.returncode not in (0, 1):  # 1 = no matches
            return self._error_result(f"ripgrep 错误: {stderr.decode()}")

        output = stdout.decode()
        if not output:
            output = "(无匹配结果)"

        match_count = len([line for line in output.splitlines() if line.strip()])

        return self._success_result(output)

    async def _grep_python(
        self,
        pattern: str,
        search_root: Path,
        glob_pattern: str | None,
        context_lines: int,
        case_sensitive: bool,
        limit: int,
        workspace_root: Path,
    ) -> ToolResult:
        """使用 Python 实现的 grep（fallback）"""
        regex_flags = 0 if case_sensitive else re.IGNORECASE
        try:
            regex = re.compile(pattern, regex_flags)
        except re.error as e:
            return self._error_result(f"正则表达式错误: {e}")

        # 确定搜索文件
        if glob_pattern:
            files = list(search_root.glob(glob_pattern))
        else:
            files = list(search_root.rglob("*"))

        files = [f for f in files if f.is_file()]

        results = []
        match_count = 0

        for file_path in files:
            if match_count >= limit:
                break

            try:
                content = file_path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, PermissionError):
                continue

            lines = content.splitlines()
            for line_num, line in enumerate(lines, start=1):
                if match_count >= limit:
                    break

                if regex.search(line):
                    rel_path = file_path.relative_to(workspace_root)

                    # 构建上下文
                    context_start = max(0, line_num - 1 - context_lines)
                    context_end = min(len(lines), line_num + context_lines)

                    if context_lines > 0:
                        context_block = []
                        for i in range(context_start, context_end):
                            prefix = ">" if i == line_num - 1 else " "
                            context_block.append(f"{prefix} {i+1}: {lines[i]}")
                        results.append(f"{rel_path}:\n" + "\n".join(context_block))
                    else:
                        results.append(f"{rel_path}:{line_num}: {line}")

                    match_count += 1

        output = "\n\n".join(results) if results else "(无匹配结果)"

        return self._success_result(output)
