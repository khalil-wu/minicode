"""
搜索工具（DESIGN.md §8.2）。

  - grep_files: 正则搜索文件内容。≤50 条匹配行。权限: AUTO
  - glob_files: 文件名模式匹配。≤100 条匹配文件。权限: AUTO

路径解析：相对路径将相对于当前工作区根目录解析（如已导入项目）。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from threading import Lock
from typing import Any

from backend.permissions.context import ToolExecutionContext
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema

GREP_MAX_MATCHES = 50
GLOB_MAX_MATCHES = 100
SEARCH_CACHE_TTL_SECONDS = 45.0
SEARCH_CACHE_MAX_ENTRIES = 128
GREP_PARALLEL_FILE_THRESHOLD = 40
GREP_MAX_WORKERS = 4
_IGNORED_PATH_PARTS = {"__pycache__", "node_modules", ".git"}

_glob_result_cache: OrderedDict[str, tuple[float, str]] = OrderedDict()
_grep_result_cache: OrderedDict[str, tuple[float, str]] = OrderedDict()
_search_cache_lock = Lock()


class PathTraversalError(ValueError):
    """Raised when a resolved path escapes the workspace boundary."""
    pass


def _resolve_search_path(path_str: str, context: Any = None) -> Path:
    """
    Resolve search path relative to workspace root if available.
    Validates that the resolved path stays within the workspace boundary.

    Raises:
        PathTraversalError: if the resolved path escapes workspace root.
    """
    workspace_root: Path | None = None
    if context and hasattr(context, "workspace_root") and context.workspace_root:
        workspace_root = Path(context.workspace_root).resolve()

    path = Path(path_str)
    if path.is_absolute():
        resolved = path.resolve()
    elif workspace_root:
        resolved = (workspace_root / path).resolve()
    else:
        resolved = path.resolve()

    if workspace_root:
        try:
            resolved.relative_to(workspace_root)
        except ValueError:
            raise PathTraversalError(
                f"路径 {path_str} 超出工作区边界 ({workspace_root})"
            )
    else:
        cwd = Path.cwd().resolve()
        try:
            resolved.relative_to(cwd)
        except ValueError:
            raise PathTraversalError(
                f"Path {path_str} is outside the current working directory."
            )

    return resolved


def _is_within_path(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except (OSError, ValueError):
        return False


def _should_ignore_parts(parts: tuple[str, ...]) -> bool:
    return any(p.startswith(".") or p in _IGNORED_PATH_PARTS for p in parts)


def _build_cache_key(prefix: str, payload: dict[str, Any]) -> str:
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return f"{prefix}:{serialized}"


def _search_cache_get(
    cache: OrderedDict[str, tuple[float, str]],
    cache_key: str,
) -> str | None:
    now = time.monotonic()
    with _search_cache_lock:
        entry = cache.get(cache_key)
        if entry is None:
            return None

        expires_at, cached_result = entry
        if now > expires_at:
            cache.pop(cache_key, None)
            return None

        cache.move_to_end(cache_key)
        return cached_result


def _search_cache_put(
    cache: OrderedDict[str, tuple[float, str]],
    cache_key: str,
    result: str,
) -> None:
    expires_at = time.monotonic() + SEARCH_CACHE_TTL_SECONDS
    with _search_cache_lock:
        cache[cache_key] = (expires_at, result)
        cache.move_to_end(cache_key)
        while len(cache) > SEARCH_CACHE_MAX_ENTRIES:
            cache.popitem(last=False)


def clear_search_caches() -> None:
    """Clear glob/grep in-memory caches."""
    with _search_cache_lock:
        _glob_result_cache.clear()
        _grep_result_cache.clear()


GREP_MAX_CANDIDATE_FILES = 10_000


def _collect_candidate_files(path: Path, file_extensions: list[str]) -> list[Path]:
    candidates: list[Path] = []
    root = path.resolve()
    for file_path in sorted(path.rglob("*")):
        if not file_path.is_file():
            continue
        parts = file_path.relative_to(path).parts
        if _should_ignore_parts(parts):
            continue
        if not _is_within_path(file_path, root):
            continue
        if file_extensions and file_path.suffix not in file_extensions:
            continue
        candidates.append(file_path)
        if len(candidates) >= GREP_MAX_CANDIDATE_FILES:
            break
    return candidates


def _grep_file_matches(file_path: Path, root_path: Path, regex: re.Pattern[str]) -> list[str]:
    if not _is_within_path(file_path, root_path):
        return []
    try:
        content = file_path.read_text(encoding="utf-8", errors="ignore")
    except (PermissionError, OSError):
        return []

    matches: list[str] = []
    rel_path = file_path.relative_to(root_path)
    for line_num, line in enumerate(content.split("\n"), 1):
        if regex.search(line):
            matches.append(f"  {rel_path}:{line_num}: {line.rstrip()}")
            if len(matches) >= GREP_MAX_MATCHES:
                break
    return matches


def _grep_candidates(
    candidate_files: list[Path],
    root_path: Path,
    regex: re.Pattern[str],
) -> tuple[list[str], int]:
    files_searched = len(candidate_files)
    if files_searched == 0:
        return [], 0

    matches: list[str] = []
    if files_searched < GREP_PARALLEL_FILE_THRESHOLD:
        for file_path in candidate_files:
            file_matches = _grep_file_matches(file_path, root_path, regex)
            if not file_matches:
                continue
            remaining = GREP_MAX_MATCHES - len(matches)
            matches.extend(file_matches[:remaining])
            if len(matches) >= GREP_MAX_MATCHES:
                break
        return matches, files_searched

    max_workers = min(GREP_MAX_WORKERS, max(1, os.cpu_count() or 1))
    worker = partial(_grep_file_matches, root_path=root_path, regex=regex)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for file_matches in executor.map(worker, candidate_files, chunksize=8):
            if not file_matches:
                continue
            remaining = GREP_MAX_MATCHES - len(matches)
            matches.extend(file_matches[:remaining])
            if len(matches) >= GREP_MAX_MATCHES:
                break

    return matches, files_searched


class GlobFilesTool(BaseTool):
    """
    Fast file pattern matching tool that works with any codebase size.
    Supports glob patterns like "**/*.js" or "src/**/*.ts"
    Returns matching file paths sorted by name.
    Permission: AUTO
    """

    name = "glob_files"
    read_only = True
    description = (
        "Fast file pattern matching tool that works with any codebase size. "
        "Supports glob patterns like '**/*.js' or 'src/**/*.ts'. "
        "Returns matching file paths. "
        "Defaults to searching in workspace root (if project imported). "
        "Use this tool when you need to find files by name patterns (e.g. searching for a missing file)."
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
                        "description": "The glob pattern to match files against (e.g. '**/*.py' or 'tests/**/*test*.py').",
                    },
                    "directory": {
                        "type": "string",
                        "description": "The directory to search in. Defaults to the current directory.",
                    },
                },
                "required": ["pattern"],
            },
        )

    async def execute(self, args: dict[str, Any], context: ToolExecutionContext | None = None) -> ToolResult:
        pattern = args.get("pattern", "")
        directory = args.get("directory", ".")

        if not pattern:
            return self._error_result("Missing 'pattern' parameter.")

        try:
            path = _resolve_search_path(directory, context)
        except PathTraversalError as exc:
            return self._error_result(str(exc))

        if not path.exists() or not path.is_dir():
            return self._error_result(f"Directory does not exist: {directory}")

        cache_key = _build_cache_key(
            "glob",
            {
                "directory": str(path),
                "pattern": pattern,
            },
        )
        cached_result = _search_cache_get(_glob_result_cache, cache_key)
        if cached_result is not None:
            return self._success_result(cached_result)

        matches: list[str] = []
        match_count = 0
        traversed = 0
        max_traversal = 50_000

        # Perform the glob matching
        try:
            for file_path in path.glob(pattern):
                traversed += 1
                if traversed > max_traversal:
                    break

                if not file_path.is_file():
                    continue

                parts = file_path.relative_to(path).parts
                if _should_ignore_parts(parts):
                    continue
                if not _is_within_path(file_path, path):
                    continue

                matches.append(str(file_path.relative_to(path) if path != file_path else file_path))
                match_count += 1
                if match_count >= GLOB_MAX_MATCHES:
                    break
        except Exception as exc:
            return self._error_result(f"Invalid glob pattern or error reading directory: {exc}")

        if not matches:
            result = f"No files matched the pattern '{pattern}' in {directory}."
            _search_cache_put(_glob_result_cache, cache_key, result)
            return self._success_result(result)

        matches.sort()
        header = f"Found {len(matches)} matching files for '{pattern}' in {directory}:"
        if match_count >= GLOB_MAX_MATCHES:
            header += f" (truncated to first {GLOB_MAX_MATCHES})"

        result = header + "\n" + "\n".join("- " + m for m in matches)
        _search_cache_put(_glob_result_cache, cache_key, result)
        return self._success_result(result)


class GrepFilesTool(BaseTool):
    """
    在目录中搜索匹配正则表达式的文件内容。

    返回匹配行的文件路径、行号和内容。最多 50 条结果。
    权限: AUTO
    """

    name = "grep_files"
    read_only = True
    description = (
        "在指定目录中搜索匹配正则表达式模式的文件内容。"
        "返回匹配行列表，包含文件路径、行号和内容。最多返回 50 条结果。"
        "默认在当前工作区根目录搜索（如已导入项目）。"
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

    async def execute(self, args: dict[str, Any], context: ToolExecutionContext | None = None) -> ToolResult:
        pattern = args.get("pattern", "")
        directory = args.get("directory", ".")
        file_extensions = args.get("file_extensions", [])
        case_insensitive = args.get("case_insensitive", False)

        if not pattern:
            return self._error_result("缺少 pattern 参数")

        try:
            path = _resolve_search_path(directory, context)
        except PathTraversalError as exc:
            return self._error_result(str(exc))

        if not path.exists():
            return self._error_result(f"目录不存在: {directory}")

        try:
            flags = re.IGNORECASE if case_insensitive else 0
            regex = re.compile(pattern, flags)
        except re.error as exc:
            return self._error_result(f"无效的正则表达式: {exc}")

        normalized_extensions = sorted({
            cleaned if cleaned.startswith(".") else f".{cleaned}"
            for raw_ext in file_extensions
            if isinstance(raw_ext, str)
            for cleaned in [raw_ext.strip()]
            if cleaned
        })

        cache_key = _build_cache_key(
            "grep",
            {
                "directory": str(path),
                "pattern": pattern,
                "file_extensions": normalized_extensions,
                "case_insensitive": bool(case_insensitive),
            },
        )
        cached_result = _search_cache_get(_grep_result_cache, cache_key)
        if cached_result is not None:
            return self._success_result(cached_result)

        candidate_files = _collect_candidate_files(path, normalized_extensions)
        matches, files_searched = await asyncio.to_thread(
            _grep_candidates,
            candidate_files,
            path,
            regex,
        )

        if not matches:
            result = f"在 {directory} 中搜索 '{pattern}'：无匹配结果（搜索了 {files_searched} 个文件）"
            _search_cache_put(_grep_result_cache, cache_key, result)
            return self._success_result(result)

        header = f"在 {directory} 中搜索 '{pattern}'：找到 {len(matches)} 处匹配"
        if len(matches) >= GREP_MAX_MATCHES:
            header += f"（已截断，上限 {GREP_MAX_MATCHES} 条）"

        result = header + "\n\n" + "\n".join(matches)
        _search_cache_put(_grep_result_cache, cache_key, result)
        return self._success_result(result)
