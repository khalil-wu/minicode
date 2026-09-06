"""
Diff 生成器（DESIGN.md §15.5）。

为 write_file / edit_file 生成 unified diff，
用于 DIFF_REVIEW 审批流。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from backend.diff.unified import count_unified_diff_changes as _count_unified_diff_changes, iter_unified_diff


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
    diff = iter_unified_diff(
        old_content,
        new_content,
        fromfile=f"a/{file_path}",
        tofile=f"b/{file_path}",
        context_lines=context_lines,
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
    try:
        old_content = path.read_bytes().decode("utf-8")
    except FileNotFoundError:
        old_content = ""
    except (UnicodeDecodeError, OSError) as exc:
        return f"无法生成差异，读取现有文件失败: {file_path}\n{exc}\n"

    return generate_unified_diff(file_path, old_content, new_content, context_lines)


def generate_edit_diff(
    file_path: str,
    old_string: str,
    new_string: str,
    context_lines: int = 3,
    *,
    replace_all: bool | str = False,
) -> str:
    """
    为 edit_file 操作生成 diff。

    先读取文件当前内容，执行替换，然后生成差异。
    """
    payload = generate_edit_diff_payload(
        file_path, old_string, new_string, context_lines, replace_all=replace_all,
    )
    return payload["files"][0]["patch"] if payload["format"] == "structured" else payload["raw"]


def build_structured_diff_payload(
    file_path: str,
    patch: str,
    *,
    status: str = "modified",
    old_path: str | None = None,
    size_bytes: int | None = None,
) -> dict[str, Any]:
    normalized_patch = patch
    if not normalized_patch.strip() and status == "modified":
        return {"format": "raw", "raw": ""}

    if normalized_patch and ("--- " not in normalized_patch or "+++ " not in normalized_patch):
        return {"format": "raw", "raw": normalized_patch}

    additions, deletions = _count_unified_diff_changes(normalized_patch)
    file_entry: dict[str, Any] = {
        "path": file_path,
        "status": status,
        "additions": additions,
        "deletions": deletions,
        "patch": normalized_patch,
    }
    if old_path is not None:
        file_entry["old_path"] = old_path
    if size_bytes is not None:
        file_entry["size_bytes"] = size_bytes

    return {
        "format": "structured",
        "stats": {
            "files_count": 1,
            "additions": additions,
            "deletions": deletions,
        },
        "files": [file_entry],
    }


def generate_file_diff_payload(
    file_path: str,
    new_content: str,
    context_lines: int = 3,
) -> dict[str, Any]:
    status = "modified" if Path(file_path).exists() else "added"
    patch = generate_file_diff(file_path, new_content, context_lines)
    return build_structured_diff_payload(
        file_path,
        patch,
        status=status,
        size_bytes=len(new_content.encode("utf-8")),
    )


def generate_edit_diff_payload(
    file_path: str,
    old_string: str,
    new_string: str,
    context_lines: int = 3,
    *,
    replace_all: bool | str = False,
) -> dict[str, Any]:
    from backend.tools.edit_file import prepare_edit_content

    try:
        current_content = Path(file_path).read_bytes().decode("utf-8")
        next_content, _ = prepare_edit_content(
            current_content, old_string, new_string,
            file_path=file_path, replace_all=replace_all,
        )
    except (OSError, ValueError) as exc:
        return {"format": "raw", "raw": f"无法生成编辑差异: {file_path}\n{exc}\n"}
    patch = generate_unified_diff(file_path, current_content, next_content, context_lines)

    return build_structured_diff_payload(
        file_path,
        patch,
        status="modified",
        size_bytes=len(next_content.encode("utf-8")),
    )
