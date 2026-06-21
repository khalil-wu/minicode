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
from backend.tools.file_tools import PathTraversalError
from backend.workspace.path_filters import is_windows_reserved_path

GREP_MAX_MATCHES = 50
GLOB_MAX_MATCHES = 100
SEARCH_CACHE_TTL_SECONDS = 45.0
SEARCH_CACHE_MAX_ENTRIES = 128
GREP_PARALLEL_FILE_THRESHOLD = 40
GREP_MAX_WORKERS = 4
_IGNORED_PATH_PARTS = {"__pycache__", "node_modules", ".git"}
_GREP_OUTPUT_MODES = {"content", "files_with_matches", "count"}
_GREP_TYPE_EXTENSIONS: dict[str, tuple[str, ...]] = {
    "py": (".py",),
    "python": (".py",),
    "js": (".js", ".jsx"),
    "javascript": (".js", ".jsx"),
    "ts": (".ts", ".tsx"),
    "typescript": (".ts", ".tsx"),
    "tsx": (".tsx",),
    "jsx": (".jsx",),
    "go": (".go",),
    "rust": (".rs",),
    "rs": (".rs",),
    "java": (".java",),
    "c": (".c", ".h"),
    "cpp": (".cc", ".cpp", ".cxx", ".hh", ".hpp", ".hxx"),
    "cs": (".cs",),
    "csharp": (".cs",),
    "rb": (".rb",),
    "ruby": (".rb",),
    "php": (".php",),
    "swift": (".swift",),
    "kt": (".kt", ".kts"),
    "kotlin": (".kt", ".kts"),
    "md": (".md", ".markdown"),
    "markdown": (".md", ".markdown"),
    "json": (".json",),
    "yaml": (".yaml", ".yml"),
    "yml": (".yaml", ".yml"),
    "toml": (".toml",),
    "html": (".html", ".htm"),
    "css": (".css",),
    "scss": (".scss",),
    "sh": (".sh", ".bash", ".zsh"),
    "shell": (".sh", ".bash", ".zsh"),
}


def _decode_process_output(data: bytes | None) -> str:
    return (data or b"").decode("utf-8", errors="replace")


def _check_ripgrep() -> bool:
    """Check if ripgrep (rg) is available on the system PATH."""
    try:
        import shutil
        return shutil.which("rg") is not None
    except Exception:
        return False


_HAS_RIPGREP = _check_ripgrep()

_glob_result_cache: OrderedDict[str, tuple[float, str]] = OrderedDict()
_grep_result_cache: OrderedDict[str, tuple[float, str]] = OrderedDict()
_search_cache_lock = Lock()


def _is_bypass_mode(context: Any = None) -> bool:
    permission = getattr(context, "permission", None)
    return getattr(permission, "mode", None) == "bypass"


def _resolve_search_path(
    path_str: str,
    context: Any = None,
    fallback_workspace_root: Path | None = None,
    *,
    allow_workspace_escape: bool = False,
) -> Path:
    """
    Resolve search path relative to workspace root if available.
    Validates that the resolved path stays within the workspace boundary.

    Resolution priority:
      1. context.workspace_root (from execution context)
      2. fallback_workspace_root (from tool constructor)
      3. Path.cwd()

    Raises:
        PathTraversalError: if the resolved path escapes workspace root.
    """
    workspace_root: Path | None = None
    if context and hasattr(context, "workspace_root") and context.workspace_root:
        workspace_root = Path(context.workspace_root).resolve()
    elif fallback_workspace_root is not None:
        workspace_root = Path(fallback_workspace_root).resolve()

    path = Path(path_str)
    if path.is_absolute():
        resolved = path.resolve()
    elif workspace_root:
        resolved = (workspace_root / path).resolve()
    else:
        resolved = path.resolve()

    if workspace_root and not allow_workspace_escape:
        try:
            resolved.relative_to(workspace_root)
        except ValueError:
            raise PathTraversalError(
                f"路径 {path_str} 超出工作区边界 ({workspace_root})"
            )
    elif not workspace_root and not allow_workspace_escape:
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
    return any(p.startswith(".") or p in _IGNORED_PATH_PARTS for p in parts) or is_windows_reserved_path(Path(*parts))


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "y", "on"}
    return bool(value)


def _coerce_nonnegative_int(value: Any, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _normalize_output_mode(value: Any) -> str:
    mode = str(value or "content").strip()
    return mode if mode in _GREP_OUTPUT_MODES else "content"


def _normalize_file_extensions(file_extensions: Any, file_type: Any = None) -> list[str]:
    raw_extensions: list[Any]
    if isinstance(file_extensions, str):
        raw_extensions = [part for part in re.split(r"[\s,]+", file_extensions) if part]
    elif isinstance(file_extensions, list):
        raw_extensions = file_extensions
    else:
        raw_extensions = []

    normalized = {
        cleaned if cleaned.startswith(".") else f".{cleaned}"
        for raw_ext in raw_extensions
        if isinstance(raw_ext, str)
        for cleaned in [raw_ext.strip()]
        if cleaned
    }

    type_key = str(file_type or "").strip().lower()
    if type_key:
        normalized.update(_GREP_TYPE_EXTENSIONS.get(type_key, ()))

    return sorted(normalized)


def _split_glob_patterns(glob_pattern: str | None) -> list[str]:
    if not glob_pattern:
        return []
    patterns: list[str] = []
    for raw in str(glob_pattern).split():
        if "{" in raw and "}" in raw:
            patterns.append(raw)
        else:
            patterns.extend(part for part in raw.split(",") if part)
    return [p for p in patterns if p]


def _relative_display_path(file_path: Path, root_path: Path) -> str:
    try:
        return str(file_path.resolve().relative_to(root_path.resolve()))
    except (OSError, ValueError):
        return str(file_path)


def _relativize_prefixed_line(line: str, root_path: Path) -> str:
    root = str(root_path.resolve())
    if line == root:
        return "."
    for separator in ("\\", "/"):
        prefix = root + separator
        if line.startswith(prefix):
            return line[len(prefix):]
    return line


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


def _grep_file_matches(
    file_path: Path,
    root_path: Path,
    regex: re.Pattern[str],
    context_lines: int = 0,
    output_mode: str = "content",
    multiline: bool = False,
) -> list[str]:
    if not _is_within_path(file_path, root_path):
        return []
    try:
        content = file_path.read_text(encoding="utf-8", errors="ignore")
    except (PermissionError, OSError):
        return []

    matches: list[str] = []
    rel_path = _relative_display_path(file_path, root_path)

    if multiline:
        regex_matches = list(regex.finditer(content))
        if not regex_matches:
            return []
        if output_mode == "files_with_matches":
            return [rel_path]
        if output_mode == "count":
            return [f"{rel_path}:{len(regex_matches)}"]

        for match in regex_matches:
            line_num = content.count("\n", 0, match.start()) + 1
            excerpt = match.group(0).strip()
            if len(excerpt) > 500:
                excerpt = excerpt[:500] + "..."
            excerpt = excerpt.replace("\r\n", "\n").replace("\n", "\\n")
            matches.append(f"  {rel_path}:{line_num}: {excerpt}")
            if len(matches) >= GREP_MAX_MATCHES:
                break
        return matches

    lines = content.split("\n")
    matching_line_numbers = [
        line_num
        for line_num, line in enumerate(lines, 1)
        if regex.search(line)
    ]

    if not matching_line_numbers:
        return []

    if output_mode == "files_with_matches":
        return [rel_path]
    if output_mode == "count":
        return [f"{rel_path}:{len(matching_line_numbers)}"]

    for line_num, line in enumerate(lines, 1):
        if regex.search(line):
            if context_lines > 0:
                ctx_start = max(0, line_num - 1 - context_lines)
                ctx_end = min(len(lines), line_num + context_lines)
                block: list[str] = []
                for i in range(ctx_start, ctx_end):
                    prefix = ">" if i == line_num - 1 else " "
                    block.append(f"  {prefix} {i + 1}: {lines[i].rstrip()}")
                matches.append(f"  {rel_path}:\n" + "\n".join(block))
            else:
                matches.append(f"  {rel_path}:{line_num}: {line.rstrip()}")
            if len(matches) >= GREP_MAX_MATCHES:
                break
    return matches


def _grep_candidates(
    candidate_files: list[Path],
    root_path: Path,
    regex: re.Pattern[str],
    context_lines: int = 0,
    output_mode: str = "content",
    multiline: bool = False,
) -> tuple[list[str], int]:
    files_searched = len(candidate_files)
    if files_searched == 0:
        return [], 0

    matches: list[str] = []
    if files_searched < GREP_PARALLEL_FILE_THRESHOLD:
        for file_path in candidate_files:
            file_matches = _grep_file_matches(
                file_path,
                root_path,
                regex,
                context_lines,
                output_mode,
                multiline,
            )
            if not file_matches:
                continue
            remaining = GREP_MAX_MATCHES - len(matches)
            matches.extend(file_matches[:remaining])
            if len(matches) >= GREP_MAX_MATCHES:
                break
        return matches, files_searched

    max_workers = min(GREP_MAX_WORKERS, max(1, os.cpu_count() or 1))
    worker = partial(
        _grep_file_matches,
        root_path=root_path,
        regex=regex,
        context_lines=context_lines,
        output_mode=output_mode,
        multiline=multiline,
    )
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for file_matches in executor.map(worker, candidate_files, chunksize=8):
            if not file_matches:
                continue
            remaining = GREP_MAX_MATCHES - len(matches)
            matches.extend(file_matches[:remaining])
            if len(matches) >= GREP_MAX_MATCHES:
                break

    return matches, files_searched


async def _grep_with_ripgrep(
    pattern: str,
    search_root: Path,
    glob_pattern: str | None = None,
    context_lines: int = 0,
    case_sensitive: bool = True,
    limit: int = GREP_MAX_MATCHES,
    output_mode: str = "content",
    multiline: bool = False,
    file_type: str | None = None,
) -> tuple[str, bool]:
    """
    Execute grep using ripgrep (rg) binary.

    Returns (output_text, is_error).
    """
    cmd = ["rg", "--color=never", "--max-columns", "500"]
    if output_mode == "files_with_matches":
        cmd.append("--files-with-matches")
    elif output_mode == "count":
        cmd.append("--count")
    else:
        cmd.extend(["--line-number", "--no-heading"])

    for reserved in ("nul", "con", "prn", "aux", "com[1-9]", "lpt[1-9]"):
        cmd.extend(["--glob", f"!**/{reserved}"])
        cmd.extend(["--glob", f"!**/{reserved}.*"])

    if multiline:
        cmd.extend(["-U", "--multiline-dotall"])

    if not case_sensitive:
        cmd.append("--ignore-case")

    if context_lines > 0 and output_mode == "content":
        cmd.extend(["-C", str(context_lines)])

    for split_pattern in _split_glob_patterns(glob_pattern):
        cmd.extend(["--glob", split_pattern])

    if file_type:
        cmd.extend(["--type", file_type])

    if output_mode == "content":
        cmd.extend(["--max-count", str(limit)])
    if pattern.startswith("-"):
        cmd.extend(["-e", pattern])
    else:
        cmd.append(pattern)
    cmd.append(str(search_root))

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    stdout, stderr = await proc.communicate()

    if proc.returncode not in (0, 1):  # 1 = no matches
        error = _decode_process_output(stderr)
        return f"ripgrep error: {error}", True

    output = _decode_process_output(stdout)
    if not output:
        output = "(no matches)"
    else:
        output_lines = [_relativize_prefixed_line(line, search_root) for line in output.splitlines()]
        if output_mode in {"files_with_matches", "count"}:
            output_lines = output_lines[:limit]
        output = "\n".join(output_lines)

    return output, False


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
        "Fast file pattern matching across the workspace. Returns paths sorted by modification time. "
        "ALWAYS use glob_files to find files by name — NEVER use run_command with find or ls. "
        "Supports: '**/*.py', 'src/**/*.ts', 'tests/**/*test*'. "
        "For content search, use grep_files."
    )
    permission = PermissionLevel.AUTO

    def __init__(self, workspace_root: Path | None = None):
        self._workspace_root = workspace_root

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "The glob pattern to match files against (e.g., '**/*.py', 'tests/**/*test*.py', 'src/**/*.ts').",
                    },
                    "directory": {
                        "type": "string",
                        "description": "The directory to search in. Defaults to the workspace root. Can be absolute or relative.",
                    },
                    "path": {
                        "type": "string",
                        "description": "Alias for directory, matching Claude Code's Glob tool parameter. Omit to search the workspace root.",
                    },
                },
                "required": ["pattern"],
            },
        )

    async def execute(self, args: dict[str, Any], context: ToolExecutionContext | None = None) -> ToolResult:
        pattern = args.get("pattern", "")
        directory = args.get("path") or args.get("directory", ".")

        if not pattern:
            return self._error_result("Missing 'pattern' parameter.")

        try:
            path = _resolve_search_path(
                directory,
                context,
                fallback_workspace_root=self._workspace_root,
                allow_workspace_escape=_is_bypass_mode(context),
            )
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

        matches: list[tuple[str, float]] = []
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

                display_path = str(file_path.relative_to(path) if path != file_path else file_path)
                try:
                    modified_at = file_path.stat().st_mtime
                except OSError:
                    modified_at = 0.0
                matches.append((display_path, modified_at))
        except Exception as exc:
            return self._error_result(f"Invalid glob pattern or error reading directory: {exc}")

        if not matches:
            result = f"No files matched the pattern '{pattern}' in {directory}."
            _search_cache_put(_glob_result_cache, cache_key, result)
            return self._success_result(result)

        matches.sort(key=lambda item: (-item[1], item[0]))
        total_matches = len(matches)
        display_matches = [match for match, _modified_at in matches[:GLOB_MAX_MATCHES]]
        header = f"Found {len(display_matches)} matching files for '{pattern}' in {directory}:"
        if total_matches > GLOB_MAX_MATCHES:
            header += f" (truncated to first {GLOB_MAX_MATCHES})"

        result = header + "\n" + "\n".join("- " + m for m in display_matches)
        _search_cache_put(_glob_result_cache, cache_key, result)
        return self._success_result(result)


class GrepFilesTool(BaseTool):
    """
    在目录中搜索匹配正则表达式的文件内容。

    返回匹配行的文件路径、行号和内容。最多 50 条结果。
    支持 ripgrep 后端（如已安装）和上下文行显示。
    权限: AUTO
    """

    name = "grep_files"
    read_only = True
    description = (
        "Search file contents using ripgrep-style regex. Returns matching lines with file paths and line numbers by default.\n\n"
        "ALWAYS use grep_files for content search — NEVER invoke grep or rg via run_command.\n"
        "Supports: full regex (e.g. 'log.*Error', 'function\\s+\\w+'), glob filter, file type filter, "
        "output modes ('content' for lines, 'files_with_matches' for paths, 'count').\n"
        "Note: literal braces need escaping — use 'interface\\{\\}' to match 'interface{}'.\n"
        "For multi-line patterns spanning lines, set multiline: true.\n"
        "For finding files by name, use glob_files."
    )
    permission = PermissionLevel.AUTO

    def __init__(self, workspace_root: Path | None = None):
        self._workspace_root = workspace_root

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Regex pattern to search for (e.g., 'def run_agent', 'TODO:', 'import .*from').",
                    },
                    "directory": {
                        "type": "string",
                        "description": "Directory to search in. Defaults to the workspace root. Can be absolute or relative.",
                    },
                    "path": {
                        "type": "string",
                        "description": "Alias for directory, matching Claude Code's Grep tool parameter. Omit to search the workspace root.",
                    },
                    "file_extensions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Limit search to specific file extensions (e.g., ['.py', '.js']). Empty or omitted searches all text files.",
                    },
                    "type": {
                        "type": "string",
                        "description": "File type to search (e.g., 'py', 'js', 'ts', 'rust', 'go'). Maps to ripgrep --type when available.",
                    },
                    "output_mode": {
                        "type": "string",
                        "enum": ["content", "files_with_matches", "count"],
                        "description": "Output mode: 'content' shows matching lines, 'files_with_matches' shows only file paths, 'count' shows per-file match counts. Defaults to 'content' for MiniCode compatibility.",
                    },
                    "case_insensitive": {
                        "type": "boolean",
                        "description": "Whether to ignore case when matching. Defaults to false.",
                    },
                    "-i": {
                        "type": "boolean",
                        "description": "Alias for case_insensitive, matching ripgrep and Claude Code Grep.",
                    },
                    "context": {
                        "type": "integer",
                        "description": "Number of context lines to show before and after each match. Defaults to 0.",
                        "default": 0,
                    },
                    "-C": {
                        "type": "integer",
                        "description": "Alias for context. Applies only with output_mode='content'.",
                    },
                    "glob": {
                        "type": "string",
                        "description": "Glob pattern to filter which files to search (e.g., '**/*.py'). Optional.",
                    },
                    "multiline": {
                        "type": "boolean",
                        "description": "Enable multiline matching so patterns can span lines. Defaults to false.",
                    },
                },
                "required": ["pattern"],
            },
        )

    async def execute(self, args: dict[str, Any], context: ToolExecutionContext | None = None) -> ToolResult:
        pattern = args.get("pattern", "")
        directory = args.get("path") or args.get("directory", ".")
        output_mode = _normalize_output_mode(args.get("output_mode"))
        file_type = str(args.get("type") or "").strip()
        file_extensions = _normalize_file_extensions(args.get("file_extensions", []), file_type)
        case_insensitive = _as_bool(args.get("-i", args.get("case_insensitive", False)))
        context_lines = _coerce_nonnegative_int(args.get("-C", args.get("context", 0)))
        glob_filter = args.get("glob")
        multiline = _as_bool(args.get("multiline", False))

        if not pattern:
            return self._error_result("缺少 pattern 参数")

        try:
            path = _resolve_search_path(
                directory,
                context,
                fallback_workspace_root=self._workspace_root,
                allow_workspace_escape=_is_bypass_mode(context),
            )
        except PathTraversalError as exc:
            return self._error_result(str(exc))

        if not path.exists():
            return self._error_result(f"目录不存在: {directory}")

        # --- Ripgrep backend (preferred when available and no extension filter) ---
        use_ripgrep = _HAS_RIPGREP and not file_extensions
        if use_ripgrep:
            cache_key = _build_cache_key(
                "grep",
                {
                    "directory": str(path),
                    "pattern": pattern,
                    "glob": glob_filter or "",
                    "output_mode": output_mode,
                    "type": file_type,
                    "case_insensitive": bool(case_insensitive),
                    "context": int(context_lines),
                    "multiline": bool(multiline),
                    "backend": "ripgrep",
                },
            )
            cached_result = _search_cache_get(_grep_result_cache, cache_key)
            if cached_result is not None:
                return self._success_result(cached_result)

            rg_output, is_error = await _grep_with_ripgrep(
                pattern=pattern,
                search_root=path,
                glob_pattern=glob_filter,
                context_lines=context_lines,
                case_sensitive=not case_insensitive,
                limit=GREP_MAX_MATCHES,
                output_mode=output_mode,
                multiline=multiline,
                file_type=file_type or None,
            )
            if is_error:
                return self._error_result(rg_output)

            header = f"在 {directory} 中搜索 '{pattern}'（模式: {output_mode}）"
            if context_lines > 0 and output_mode == "content":
                header += f"（上下文 {context_lines} 行）"
            result = header + "\n\n" + rg_output
            _search_cache_put(_grep_result_cache, cache_key, result)
            return self._success_result(result)

        # --- Python fallback backend ---
        try:
            flags = re.IGNORECASE if case_insensitive else 0
            if multiline:
                flags |= re.DOTALL | re.MULTILINE
            regex = re.compile(pattern, flags)
        except re.error as exc:
            return self._error_result(f"无效的正则表达式: {exc}")

        cache_key = _build_cache_key(
            "grep",
            {
                "directory": str(path),
                "pattern": pattern,
                "file_extensions": file_extensions,
                "case_insensitive": bool(case_insensitive),
                "context": int(context_lines),
                "glob": glob_filter or "",
                "output_mode": output_mode,
                "multiline": bool(multiline),
                "backend": "python",
            },
        )
        cached_result = _search_cache_get(_grep_result_cache, cache_key)
        if cached_result is not None:
            return self._success_result(cached_result)

        candidate_files = _collect_candidate_files(path, file_extensions)

        # Apply glob pattern filter in Python fallback when specified
        if glob_filter:
            candidate_files = [
                f for f in candidate_files
                if f.match(glob_filter)
            ]
        matches, files_searched = await asyncio.to_thread(
            _grep_candidates,
            candidate_files,
            path,
            regex,
            context_lines,
            output_mode,
            multiline,
        )

        if not matches:
            result = f"在 {directory} 中搜索 '{pattern}'：无匹配结果（搜索了 {files_searched} 个文件）"
            _search_cache_put(_grep_result_cache, cache_key, result)
            return self._success_result(result)

        header = f"在 {directory} 中搜索 '{pattern}'：找到 {len(matches)} 条结果（模式: {output_mode}）"
        if len(matches) >= GREP_MAX_MATCHES:
            header += f"（已截断，上限 {GREP_MAX_MATCHES} 条）"
        if context_lines > 0 and output_mode == "content":
            header += f"（上下文 {context_lines} 行）"

        result = header + "\n\n" + "\n".join(matches)
        _search_cache_put(_grep_result_cache, cache_key, result)
        return self._success_result(result)
