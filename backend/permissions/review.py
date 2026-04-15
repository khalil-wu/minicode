"""
Diff 生成器（DESIGN.md §15.5）。

为 write_file / edit_file 生成 unified diff，
用于 DIFF_REVIEW 审批流。
"""

from __future__ import annotations

import difflib
from pathlib import Path


def generate_unified_diff(
    file_path: str,
    old_content: str,
    new_content: str,
    context_lines: int = 3,
) -> str:
    """
    生成 unified diff 格式的差异。

    Args:
        file_path: 文件路径（用于 diff header）
        old_content: 修改前的内容
        new_content: 修改后的内容
        context_lines: 上下文行数

    Returns:
        unified diff 字符串
    """
    old_lines = old_content.splitlines(keepends=True)
    new_lines = new_content.splitlines(keepends=True)

    diff = difflib.unified_diff(
        old_lines,
        new_lines,
        fromfile=f"a/{file_path}",
        tofile=f"b/{file_path}",
        n=context_lines,
    )

    return "".join(diff)


def generate_file_diff(
    file_path: str,
    new_content: str,
    context_lines: int = 3,
) -> str:
    """
    对比文件当前内容和新内容，生成 diff。

    如果文件不存在，视为从空文件创建。
    """
    path = Path(file_path)
    if path.exists():
        try:
            old_content = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, PermissionError):
            old_content = ""
    else:
        old_content = ""

    return generate_unified_diff(file_path, old_content, new_content, context_lines)


def generate_edit_diff(
    file_path: str,
    old_string: str,
    new_string: str,
    context_lines: int = 3,
) -> str:
    """
    为 edit_file 操作生成 diff。

    先读取文件当前内容，执行替换，然后生成差异。
    """
    path = Path(file_path)
    if not path.exists():
        return f"--- 文件不存在: {file_path}\n"

    try:
        current_content = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, PermissionError):
        return f"--- 无法读取文件: {file_path}\n"

    new_content = current_content.replace(old_string, new_string, 1)
    return generate_unified_diff(file_path, current_content, new_content, context_lines)
