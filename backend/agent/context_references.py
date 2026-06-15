"""
Context References - @Mention 引用系统

支持 Claude Code 风格的 @ 引用语法:
- @file:path/to/file.py
- @file:path/to/file.py:10-25
- @folder:path/to/dir
- @diff
- @staged
- @git:5
- @url:https://example.com

参考: .codex/reference-sources/hermes-agent/website/docs/user-guide/features/context-references.md
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# 引用语法正则
REFERENCE_PATTERN = re.compile(
    r'@(?P<type>file|folder|diff|staged|git|url):?(?P<value>[^\s,;!?.\n]+)?',
    re.IGNORECASE
)

# 敏感路径黑名单（安全保护）
SENSITIVE_PATHS = {
    '.ssh/id_rsa',
    '.ssh/id_ed25519',
    '.ssh/authorized_keys',
    '.ssh/config',
    '.aws/credentials',
    '.aws/config',
    '.env',
    '.env.local',
    '.env.production',
    'credentials.json',
    'secrets.yaml',
    '.npmrc',
    '.pypirc',
    '.netrc',
    '.pgpass',
}

SENSITIVE_DIRS = {
    '.ssh',
    '.aws',
    '.gnupg',
    '.kube',
}


@dataclass
class Reference:
    """解析后的引用"""
    type: str  # file, folder, diff, staged, git, url
    value: str  # 引用值（路径、URL 等）
    original: str  # 原始文本


@dataclass
class ExpandedReference:
    """扩展后的引用内容"""
    reference: Reference
    content: str
    error: Optional[str] = None
    truncated: bool = False
    size_bytes: int = 0


def parse_references(message: str) -> list[Reference]:
    """从消息中提取所有 @references

    Args:
        message: 用户消息

    Returns:
        引用列表

    Examples:
        >>> parse_references("Check @file:main.py and @diff")
        [Reference(type='file', value='main.py', ...),
         Reference(type='diff', value='', ...)]
    """
    references = []

    for match in REFERENCE_PATTERN.finditer(message):
        ref_type = match.group('type').lower()
        ref_value = (match.group('value') or '').rstrip('.,;!?')
        original = match.group(0)

        references.append(Reference(
            type=ref_type,
            value=ref_value,
            original=original
        ))

    return references


def is_sensitive_path(path: Path) -> bool:
    """检查路径是否为敏感文件

    Args:
        path: 文件路径

    Returns:
        是否敏感
    """
    # 检查敏感目录
    for part in path.parts:
        if part in SENSITIVE_DIRS:
            return True

    # 检查敏感文件
    path_str = path.as_posix()
    for sensitive in SENSITIVE_PATHS:
        if sensitive in path_str:
            return True

    return False


def is_binary_file(path: Path) -> bool:
    """检查是否为二进制文件

    Args:
        path: 文件路径

    Returns:
        是否二进制
    """
    # 已知文本扩展名
    text_extensions = {
        '.py', '.js', '.ts', '.tsx', '.jsx',
        '.java', '.go', '.rs', '.cpp', '.c', '.h',
        '.md', '.txt', '.json', '.yaml', '.yml',
        '.toml', '.xml', '.html', '.css', '.scss',
        '.sh', '.bash', '.zsh', '.ps1',
    }

    if path.suffix.lower() in text_extensions:
        return False

    # 读取前 8192 字节检查 null 字节
    try:
        with open(path, 'rb') as f:
            chunk = f.read(8192)
            return b'\x00' in chunk
    except Exception:
        return True


async def expand_file_reference(
    ref_value: str,
    workspace_root: Path,
    max_size: int = 100_000,
) -> ExpandedReference:
    """扩展 @file: 引用

    Args:
        ref_value: 文件路径，可能包含行范围 (file.py:10-25)
        workspace_root: 工作区根目录
        max_size: 最大文件大小（字节）

    Returns:
        扩展后的内容
    """
    ref = Reference(type='file', value=ref_value, original=f'@file:{ref_value}')

    # 解析行范围
    if ':' in ref_value:
        path_str, line_range = ref_value.rsplit(':', 1)
    else:
        path_str = ref_value
        line_range = None

    try:
        # 解析路径
        file_path = workspace_root / path_str

        # 安全检查
        if not file_path.exists():
            return ExpandedReference(
                reference=ref,
                content='',
                error=f'File not found: {path_str}'
            )

        if is_sensitive_path(file_path):
            return ExpandedReference(
                reference=ref,
                content='',
                error=f'Blocked: {path_str} is a sensitive credential file'
            )

        if is_binary_file(file_path):
            return ExpandedReference(
                reference=ref,
                content='',
                error=f'Binary files are not supported: {path_str}'
            )

        # 读取文件
        content = file_path.read_text(encoding='utf-8', errors='replace')
        size_bytes = len(content.encode('utf-8'))

        # 截断大文件
        truncated = False
        if size_bytes > max_size:
            content = content[:max_size]
            truncated = True

        # 行范围截取
        if line_range:
            lines = content.split('\n')
            start, end = parse_line_range(line_range, len(lines))
            content = '\n'.join(lines[start-1:end])

        # 格式化输出
        formatted = f"## @file:{ref_value}\n\n```{file_path.suffix[1:]}\n{content}\n```"

        if truncated:
            formatted += f"\n\n⚠️ File truncated (showed {max_size // 1024}KB of {size_bytes // 1024}KB)"

        return ExpandedReference(
            reference=ref,
            content=formatted,
            truncated=truncated,
            size_bytes=size_bytes
        )

    except Exception as e:
        logger.warning(f"Failed to expand @file:{ref_value}: {e}")
        return ExpandedReference(
            reference=ref,
            content='',
            error=f'Error reading file: {str(e)}'
        )


def parse_line_range(line_range: str, total_lines: int) -> tuple[int, int]:
    """解析行范围字符串

    Args:
        line_range: 行范围 (例如 "10", "10-25")
        total_lines: 文件总行数

    Returns:
        (start, end) 1-indexed, inclusive
    """
    if '-' in line_range:
        start_str, end_str = line_range.split('-', 1)
        start = int(start_str)
        end = int(end_str)
    else:
        start = end = int(line_range)

    # 范围校验
    start = max(1, min(start, total_lines))
    end = max(start, min(end, total_lines))

    return start, end


async def expand_folder_reference(
    ref_value: str,
    workspace_root: Path,
    max_entries: int = 200,
) -> ExpandedReference:
    """扩展 @folder: 引用

    Args:
        ref_value: 文件夹路径
        workspace_root: 工作区根目录
        max_entries: 最大条目数

    Returns:
        扩展后的内容
    """
    ref = Reference(type='folder', value=ref_value, original=f'@folder:{ref_value}')

    try:
        folder_path = workspace_root / ref_value

        if not folder_path.exists():
            return ExpandedReference(
                reference=ref,
                content='',
                error=f'Folder not found: {ref_value}'
            )

        if not folder_path.is_dir():
            return ExpandedReference(
                reference=ref,
                content='',
                error=f'Not a directory: {ref_value}'
            )

        # 递归列出文件
        entries = []
        for item in folder_path.rglob('*'):
            if item.is_file():
                rel_path = item.relative_to(folder_path)
                size = item.stat().st_size
                entries.append((rel_path, size))

                if len(entries) >= max_entries:
                    break

        # 格式化输出
        lines = [f"## @folder:{ref_value}\n"]
        lines.append(f"Total: {len(entries)} files\n")

        for rel_path, size in sorted(entries):
            size_kb = size / 1024
            lines.append(f"- {rel_path} ({size_kb:.1f} KB)")

        if len(entries) >= max_entries:
            lines.append(f"\n... (truncated at {max_entries} entries)")

        content = '\n'.join(lines)

        return ExpandedReference(
            reference=ref,
            content=content,
            truncated=len(entries) >= max_entries,
            size_bytes=len(content.encode('utf-8'))
        )

    except Exception as e:
        logger.warning(f"Failed to expand @folder:{ref_value}: {e}")
        return ExpandedReference(
            reference=ref,
            content='',
            error=f'Error listing folder: {str(e)}'
        )


async def expand_diff_reference(workspace_root: Path) -> ExpandedReference:
    """扩展 @diff 引用（git diff 未暂存）

    Args:
        workspace_root: 工作区根目录

    Returns:
        扩展后的内容
    """
    ref = Reference(type='diff', value='', original='@diff')

    try:
        result = subprocess.run(
            ['git', 'diff'],
            cwd=workspace_root,
            capture_output=True,
            text=True,
            timeout=10
        )

        if result.returncode != 0:
            return ExpandedReference(
                reference=ref,
                content='',
                error=f'git diff failed: {result.stderr}'
            )

        diff_output = result.stdout.strip()

        if not diff_output:
            return ExpandedReference(
                reference=ref,
                content='## @diff\n\nNo unstaged changes.',
                size_bytes=0
            )

        content = f"## @diff\n\n```diff\n{diff_output}\n```"

        return ExpandedReference(
            reference=ref,
            content=content,
            size_bytes=len(diff_output.encode('utf-8'))
        )

    except Exception as e:
        logger.warning(f"Failed to expand @diff: {e}")
        return ExpandedReference(
            reference=ref,
            content='',
            error=f'Error running git diff: {str(e)}'
        )


async def expand_staged_reference(workspace_root: Path) -> ExpandedReference:
    """扩展 @staged 引用（git diff --staged）

    Args:
        workspace_root: 工作区根目录

    Returns:
        扩展后的内容
    """
    ref = Reference(type='staged', value='', original='@staged')

    try:
        result = subprocess.run(
            ['git', 'diff', '--staged'],
            cwd=workspace_root,
            capture_output=True,
            text=True,
            timeout=10
        )

        if result.returncode != 0:
            return ExpandedReference(
                reference=ref,
                content='',
                error=f'git diff --staged failed: {result.stderr}'
            )

        diff_output = result.stdout.strip()

        if not diff_output:
            return ExpandedReference(
                reference=ref,
                content='## @staged\n\nNo staged changes.',
                size_bytes=0
            )

        content = f"## @staged\n\n```diff\n{diff_output}\n```"

        return ExpandedReference(
            reference=ref,
            content=content,
            size_bytes=len(diff_output.encode('utf-8'))
        )

    except Exception as e:
        logger.warning(f"Failed to expand @staged: {e}")
        return ExpandedReference(
            reference=ref,
            content='',
            error=f'Error running git diff --staged: {str(e)}'
        )


async def expand_git_reference(
    ref_value: str,
    workspace_root: Path,
    max_commits: int = 10,
) -> ExpandedReference:
    """扩展 @git:N 引用（最近 N 次提交）

    Args:
        ref_value: 提交数量（字符串）
        workspace_root: 工作区根目录
        max_commits: 最大提交数

    Returns:
        扩展后的内容
    """
    ref = Reference(type='git', value=ref_value, original=f'@git:{ref_value}')

    try:
        # 解析提交数量
        n = int(ref_value or '5')
        n = max(1, min(n, max_commits))

        result = subprocess.run(
            ['git', 'log', f'-{n}', '-p'],
            cwd=workspace_root,
            capture_output=True,
            text=True,
            timeout=30
        )

        if result.returncode != 0:
            return ExpandedReference(
                reference=ref,
                content='',
                error=f'git log failed: {result.stderr}'
            )

        log_output = result.stdout.strip()

        if not log_output:
            return ExpandedReference(
                reference=ref,
                content=f'## @git:{n}\n\nNo commits found.',
                size_bytes=0
            )

        content = f"## @git:{n}\n\n```\n{log_output}\n```"

        return ExpandedReference(
            reference=ref,
            content=content,
            size_bytes=len(log_output.encode('utf-8'))
        )

    except ValueError:
        return ExpandedReference(
            reference=ref,
            content='',
            error=f'Invalid commit count: {ref_value}'
        )
    except Exception as e:
        logger.warning(f"Failed to expand @git:{ref_value}: {e}")
        return ExpandedReference(
            reference=ref,
            content='',
            error=f'Error running git log: {str(e)}'
        )


async def expand_url_reference(ref_value: str, max_size: int = 50_000) -> ExpandedReference:
    """扩展 @url: 引用（网页内容）

    Args:
        ref_value: URL
        max_size: 最大内容大小

    Returns:
        扩展后的内容
    """
    ref = Reference(type='url', value=ref_value, original=f'@url:{ref_value}')

    try:
        # 使用 WebFetch 工具（如果可用）
        from backend.tools.web_tools import fetch_url_content

        content = await fetch_url_content(ref_value, max_size=max_size)

        if not content:
            return ExpandedReference(
                reference=ref,
                content='',
                error='No content extracted from URL'
            )

        formatted = f"## @url:{ref_value}\n\n{content}"

        return ExpandedReference(
            reference=ref,
            content=formatted,
            size_bytes=len(content.encode('utf-8'))
        )

    except ImportError:
        return ExpandedReference(
            reference=ref,
            content='',
            error='URL fetching is not available (web_tools not found)'
        )
    except Exception as e:
        logger.warning(f"Failed to expand @url:{ref_value}: {e}")
        return ExpandedReference(
            reference=ref,
            content='',
            error=f'Error fetching URL: {str(e)}'
        )


async def expand_reference(
    reference: Reference,
    workspace_root: Path,
) -> ExpandedReference:
    """扩展单个引用

    Args:
        reference: 引用对象
        workspace_root: 工作区根目录

    Returns:
        扩展后的内容
    """
    ref_type = reference.type
    ref_value = reference.value

    if ref_type == 'file':
        return await expand_file_reference(ref_value, workspace_root)
    elif ref_type == 'folder':
        return await expand_folder_reference(ref_value, workspace_root)
    elif ref_type == 'diff':
        return await expand_diff_reference(workspace_root)
    elif ref_type == 'staged':
        return await expand_staged_reference(workspace_root)
    elif ref_type == 'git':
        return await expand_git_reference(ref_value, workspace_root)
    elif ref_type == 'url':
        return await expand_url_reference(ref_value)
    else:
        return ExpandedReference(
            reference=reference,
            content='',
            error=f'Unknown reference type: {ref_type}'
        )


async def expand_all_references(
    message: str,
    workspace_root: Path,
    context_limit: int = 200_000,
) -> tuple[str, list[ExpandedReference]]:
    """扩展消息中的所有引用

    Args:
        message: 用户消息
        workspace_root: 工作区根目录
        context_limit: 上下文token限制

    Returns:
        (扩展后的消息, 扩展列表)
    """
    references = parse_references(message)

    if not references:
        return message, []

    # 扩展所有引用
    expanded_list = []
    total_tokens = 0

    for ref in references:
        expanded = await expand_reference(ref, workspace_root)
        expanded_list.append(expanded)

        # 估算 token 数量（简单：字符数 / 3）
        tokens = len(expanded.content.encode('utf-8')) // 3
        total_tokens += tokens

    # 检查是否超过限制
    soft_limit = int(context_limit * 0.25)  # 25% 软限制
    hard_limit = int(context_limit * 0.50)  # 50% 硬限制

    if total_tokens > hard_limit:
        # 超过硬限制，拒绝扩展
        logger.warning(
            f"@ context injection refused: {total_tokens} tokens exceeds "
            f"the 50% hard limit ({hard_limit})."
        )
        return message, []

    # 构建附加上下文
    sections = []

    for expanded in expanded_list:
        if expanded.error:
            sections.append(f"⚠️ {expanded.reference.original} - {expanded.error}")
        elif expanded.content:
            sections.append(expanded.content)

    if not sections:
        return message, expanded_list

    # 附加到消息末尾
    attached_context = "\n\n".join(sections)
    expanded_message = f"{message}\n\n--- Attached Context ---\n\n{attached_context}"

    # 添加警告（如果超过软限制）
    if total_tokens > soft_limit:
        warning = (
            f"\n\n⚠️ Warning: attached context ({total_tokens} tokens) "
            f"exceeds 25% of context limit. Consider using line ranges "
            f"(@file:main.py:10-25) to inject only relevant sections."
        )
        expanded_message += warning

    return expanded_message, expanded_list


def format_reference_summary(expanded_list: list[ExpandedReference]) -> str:
    """格式化引用摘要（用于日志）

    Args:
        expanded_list: 扩展后的引用列表

    Returns:
        摘要字符串
    """
    if not expanded_list:
        return "No references"

    parts = []
    for expanded in expanded_list:
        ref = expanded.reference
        if expanded.error:
            parts.append(f"{ref.original} [ERROR]")
        else:
            size_kb = expanded.size_bytes / 1024
            parts.append(f"{ref.original} ({size_kb:.1f}KB)")

    return ", ".join(parts)

