"""
文件操作工具（DESIGN.md §8.2）。

四个工具：
  - read_file:  读文件内容。2K tokens 内直接返回，超出存 artifact。权限: AUTO
  - write_file: 写文件。权限: DIFF_REVIEW
  - edit_file:  精确字符串替换（old_string→new_string）。权限: DIFF_REVIEW
  - list_files: 列目录。≤100 条。权限: AUTO
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from backend.artifact.store import ArtifactStore
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema

# Token 上限常量
READ_FILE_TOKEN_LIMIT = 2000  # 约 8000 字符
LIST_FILES_MAX_ENTRIES = 100


class ReadFileTool(BaseTool):
    """
    读取文件内容。

    ≤ 2K tokens 直接返回；超出时存入 Artifact Store，只在
    context 中保留文件信息摘要 + artifact 引用。
    """

    name = "read_file"
    description = (
        "读取指定文件的文本内容。"
        "返回文件内容或当文件过大时返回摘要+artifact引用。"
        "示例: read_file(file_path='/project/src/main.py')。"
        "注意: 二进制文件（图片/视频等）无法读取。"
    )
    permission = PermissionLevel.AUTO

    def __init__(self, artifact_store: ArtifactStore) -> None:
        self._artifact_store = artifact_store

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "要读取的文件的绝对路径或相对路径",
                    },
                    "start_line": {
                        "type": "integer",
                        "description": "起始行号（1-indexed，可选）",
                    },
                    "end_line": {
                        "type": "integer",
                        "description": "结束行号（1-indexed，包含，可选）",
                    },
                },
                "required": ["file_path"],
            },
            strict=True,
        )

    async def execute(self, args: dict[str, Any]) -> ToolResult:
        file_path = args.get("file_path", "")
        start_line = args.get("start_line")
        end_line = args.get("end_line")

        if not file_path:
            return self._error_result("缺少 file_path 参数")

        path = Path(file_path)
        if not path.exists():
            return self._error_result(f"文件不存在: {file_path}")

        if not path.is_file():
            return self._error_result(f"不是一个文件: {file_path}")

        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return self._error_result(
                f"无法读取二进制文件: {file_path}。此工具仅支持文本文件。"
            )
        except PermissionError:
            return self._error_result(f"没有读取权限: {file_path}")

        # 行号范围截取
        if start_line is not None or end_line is not None:
            lines = content.split("\n")
            start = max(1, start_line or 1) - 1  # 转为 0-indexed
            end = min(len(lines), end_line or len(lines))
            content = "\n".join(lines[start:end])

        # Token 控制：超出阈值时存 artifact
        estimated_tokens = len(content) // 4
        if estimated_tokens <= READ_FILE_TOKEN_LIMIT:
            return self._success_result(content)

        # 大文件：存 artifact，返回摘要
        artifact_id = self._artifact_store.save(
            content=content,
            source=f"read_file({file_path})",
            type="code" if path.suffix in ('.py', '.js', '.ts', '.tsx', '.jsx', '.go', '.rs') else "text",
        )
        total_lines = len(content.split("\n"))
        preview = self._artifact_store.get_preview(artifact_id, lines=10)

        return self._success_result(
            content=f"文件 {file_path}（{total_lines} 行，约 {estimated_tokens} tokens）已读取。",
            artifact_id=artifact_id,
            artifact_preview=preview,
        )


class WriteFileTool(BaseTool):
    """
    写入文件。如果目标文件已存在则覆盖。

    权限: DIFF_REVIEW — 执行前需展示内容变更供用户审批。
    """

    name = "write_file"
    description = (
        "写入指定文件。如果目标文件已存在则覆盖内容。"
        "如果父目录不存在会自动创建。"
        "示例: write_file(file_path='src/utils.py', content='def hello(): pass')。"
        "注意: 适用于创建新文件或大段重写。小范围修改请使用 edit_file。"
    )
    permission = PermissionLevel.DIFF_REVIEW

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "要写入的文件路径",
                    },
                    "content": {
                        "type": "string",
                        "description": "要写入的完整内容",
                    },
                },
                "required": ["file_path", "content"],
            },
            strict=True,
        )

    async def execute(self, args: dict[str, Any]) -> ToolResult:
        file_path = args.get("file_path", "")
        content = args.get("content", "")

        if not file_path:
            return self._error_result("缺少 file_path 参数")

        path = Path(file_path)

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        except PermissionError:
            return self._error_result(f"没有写入权限: {file_path}")
        except OSError as exc:
            return self._error_result(f"写入失败: {exc}")

        total_lines = len(content.split("\n"))
        return self._success_result(
            f"已写入 {file_path}（{total_lines} 行，{len(content)} 字符）"
        )


class EditFileTool(BaseTool):
    """
    对文件进行精确的字符串替换。

    old_string 必须在文件中唯一存在，否则报错。
    权限: DIFF_REVIEW
    """

    name = "edit_file"
    description = (
        "对文件进行精确的字符串替换。"
        "old_string 必须是文件中唯一存在的字符串，否则报错。"
        "示例: 将函数名从 get_user 改为 fetch_user。"
        "注意: 不适用于大段重写，大段修改请用 write_file。"
    )
    permission = PermissionLevel.DIFF_REVIEW

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "要编辑的文件的绝对路径",
                    },
                    "old_string": {
                        "type": "string",
                        "description": "要替换的原始字符串（必须在文件中唯一存在）",
                    },
                    "new_string": {
                        "type": "string",
                        "description": "替换后的新字符串",
                    },
                },
                "required": ["file_path", "old_string", "new_string"],
            },
            strict=True,
        )

    async def execute(self, args: dict[str, Any]) -> ToolResult:
        file_path = args.get("file_path", "")
        old_string = args.get("old_string", "")
        new_string = args.get("new_string", "")

        if not file_path:
            return self._error_result("缺少 file_path 参数")
        if not old_string:
            return self._error_result("缺少 old_string 参数")

        path = Path(file_path)
        if not path.exists():
            return self._error_result(f"文件不存在: {file_path}")

        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return self._error_result(f"无法读取二进制文件: {file_path}")

        # 唯一性检查
        count = content.count(old_string)
        if count == 0:
            return self._error_result(
                f"在 {file_path} 中找不到指定的 old_string。"
                "请确认字符串完全一致（包括空格和换行）。"
            )
        if count > 1:
            return self._error_result(
                f"在 {file_path} 中找到 {count} 处匹配。"
                "old_string 必须在文件中唯一存在。请提供更长的上下文以精确定位。"
            )

        # 执行替换
        new_content = content.replace(old_string, new_string, 1)

        try:
            path.write_text(new_content, encoding="utf-8")
        except PermissionError:
            return self._error_result(f"没有写入权限: {file_path}")

        return self._success_result(
            f"已编辑 {file_path}: 替换了 {len(old_string)} 个字符为 {len(new_string)} 个字符"
        )


class ListFilesTool(BaseTool):
    """
    列出目录内容。最多返回 100 条，超出时截断并提示。

    权限: AUTO
    """

    name = "list_files"
    description = (
        "列出指定目录下的文件和子目录。"
        "返回文件名列表，标注文件/目录类型和大小。"
        "最多返回 100 条结果。"
        "示例: list_files(directory='./src')。"
    )
    permission = PermissionLevel.AUTO

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "directory": {
                        "type": "string",
                        "description": "要列出内容的目录路径",
                    },
                    "recursive": {
                        "type": "boolean",
                        "description": "是否递归列出子目录内容，默认 false",
                    },
                },
                "required": ["directory"],
            },
        )

    async def execute(self, args: dict[str, Any]) -> ToolResult:
        directory = args.get("directory", ".")
        recursive = args.get("recursive", False)

        path = Path(directory)
        if not path.exists():
            return self._error_result(f"目录不存在: {directory}")
        if not path.is_dir():
            return self._error_result(f"不是目录: {directory}")

        entries: list[str] = []
        try:
            if recursive:
                for item in sorted(path.rglob("*")):
                    # 跳过隐藏文件和 __pycache__
                    parts = item.relative_to(path).parts
                    if any(p.startswith(".") or p == "__pycache__" for p in parts):
                        continue
                    rel = item.relative_to(path)
                    if item.is_dir():
                        entries.append(f"  {rel}/")
                    else:
                        size = item.stat().st_size
                        entries.append(f"  {rel}  ({_format_size(size)})")
                    if len(entries) >= LIST_FILES_MAX_ENTRIES:
                        break
            else:
                for item in sorted(path.iterdir()):
                    if item.name.startswith(".") or item.name == "__pycache__":
                        continue
                    if item.is_dir():
                        entries.append(f"  {item.name}/")
                    else:
                        size = item.stat().st_size
                        entries.append(f"  {item.name}  ({_format_size(size)})")
                    if len(entries) >= LIST_FILES_MAX_ENTRIES:
                        break
        except PermissionError:
            return self._error_result(f"没有目录访问权限: {directory}")

        total = len(entries)
        header = f"{directory}/ ({total} 项)"
        if total >= LIST_FILES_MAX_ENTRIES:
            header += f" [截断，超过 {LIST_FILES_MAX_ENTRIES} 条上限]"

        result = header + "\n" + "\n".join(entries)
        return self._success_result(result)


def _format_size(size: int) -> str:
    """格式化文件大小。"""
    if size < 1024:
        return f"{size} B"
    elif size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    else:
        return f"{size / (1024 * 1024):.1f} MB"
