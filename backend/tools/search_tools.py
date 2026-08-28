"""
搜索工具（DESIGN.md §8.2）。

  - grep_files: 正则搜索文件内容。默认 ≤100 条匹配行（0=unlimited）。权限: AUTO
  - glob_files: 文件名模式匹配。默认 ≤1000 条匹配文件（0=unlimited）。权限: AUTO

路径解析：相对路径将相对于当前工作区根目录解析（如已导入项目）。
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

from backend.permissions.context import ToolExecutionContext
from backend.subprocesses import (
    SubprocessOutputLimitError,
    communicate_bounded,
    spawn_exec,
)
from backend.tools.base import (
    BaseTool,
    PermissionLevel,
    MAX_TOOL_RESULT_BYTES,
    MAX_TOOL_RESULT_LINES,
    ToolResult,
    ToolSchema,
    truncate_tool_result,
)
from backend.tools.contracts import ToolSpec
from backend.tools.path_resolution import (
    PathTraversalError,
    _is_declared_readable_path,
    denied_path_patterns,
)
from backend.workspace.path_filters import is_windows_reserved_path

try:
    import regex as _safe_regex
except ImportError:  # pragma: no cover - declared dependency, defensive fallback
    _safe_regex = re

# Default result budget for a grep. 250 keeps large-codebase searches useful
# without flooding one tool result.
GLOB_MAX_MATCHES = 100
class RegexSafetyLimitError(RuntimeError):
    """Raised when a model-supplied regex exceeds the per-operation budget."""


class SearchResourceLimitError(RuntimeError):
    """Raised when the Python fallback cannot complete within its bounds."""


@dataclass
class _FileGrepResult:
    matches: list[str]
    output_limit_reached: bool = False
    lines_truncated: bool = False
    context_truncated: bool = False


@dataclass
class _GrepBatchResult:
    matches: list[str]
    files_searched: int
    output_limit_reached: bool = False
    lines_truncated: bool = False
    context_truncated: bool = False


@dataclass
class _GlobFallbackResult:
    matches: list[str]
    truncated: bool
    output_limit_reached: bool = False


_NESTED_QUANTIFIER_RE = re.compile(
    r"\((?:[^()\\]|\\.){0,512}(?:[*+]|\{\d+(?:,\d*)?\})(?:[^()\\]|\\.){0,512}\)"
    r"\s*(?:[*+]|\{\d+(?:,\d*)?\})",
)


from backend.tools.search_support import (
    _apply_pagination,
    _as_bool,
    _bounded_search_output,
    _coerce_head_limit,
    _coerce_nonnegative_int,
    _denied_path_patterns,
    _denylist_ripgrep_globs,
    _glob_with_python,
    _glob_with_ripgrep,
    _grep_candidates,
    _is_bypass_mode,
    _iter_candidate_files,
    _normalize_file_extensions,
    _normalize_output_mode,
    _pagination_suffix,
    _resolve_search_path,
    _stdlib_regex_pattern_is_unsafe,

    GREP_DISPLAY_LINE_MAX_CHARS,
    _HAS_RIPGREP,
    GREP_MAX_MATCHES,
    _grep_with_ripgrep,)

class GlobFilesTool(BaseTool):
    """
    Fast file pattern matching tool that works with any codebase size.
    Supports glob patterns like "**/*.js" or "src/**/*.ts"
    Returns matching file paths sorted by name.
    Permission: AUTO
    """

    name = "glob_files"
    read_only = True
    result_kind = "file"
    activity_kind = "workspaceSearch"
    display_label = "Search"
    description = (
        "Fast file-name glob search across the workspace; returns paths sorted by modification time. "
        "Use for name patterns like '**/*.py' or 'src/**/*.ts'. For content search, use grep_files."
    )
    permission = PermissionLevel.AUTO
    workspace_path_fields = ("path", "directory")
    allow_workspace_root_path = True

    def __init__(self, workspace_root: Path | None = None):
        self._workspace_root = workspace_root

    def model_description(self) -> str:
        return "Glob-search file names in the workspace. Use grep_files for content search."

    def model_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.model_description(),
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Glob such as '**/*.py'."},
                    "path": {
                        "type": "string",
                        "description": "Directory to search; defaults to workspace root.",
                    },
                    "head_limit": {
                        "type": "integer",
                        "description": "Max paths to return; 0 for unlimited.",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Skip this many paths; pair with head_limit to page.",
                    },
                },
                "required": ["pattern"],
            },
        )

    def get_spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            capability="workspace.glob",
            toolset="core",
            exposure="core",
            required_args=("pattern",),
        )

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Glob pattern, e.g. '**/*.py' or 'src/**/*.ts'.",
                    },
                    "directory": {
                        "type": "string",
                        "description": "Directory to search; defaults to workspace root.",
                    },
                    "path": {
                        "type": "string",
                        "description": "Alias for directory.",
                    },
                    "head_limit": {
                        "type": "integer",
                        "description": f"Max matching paths. Default {GLOB_MAX_MATCHES}; 0 for unlimited.",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Skip this many paths; use with head_limit for pagination.",
                    },
                },
                "required": ["pattern"],
            },
        )

    async def execute(self, args: dict[str, Any], context: ToolExecutionContext | None = None) -> ToolResult:
        pattern = args.get("pattern", "")
        directory = args.get("path") or args.get("directory", ".")
        head_limit = _coerce_head_limit(args.get("head_limit"), GLOB_MAX_MATCHES)
        offset = _coerce_nonnegative_int(args.get("offset"), 0)

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

        denied_patterns = _denied_path_patterns(context)
        checker = getattr(context, "permission_checker", None) if context is not None else None
        permission = getattr(context, "permission", None) if context is not None else None

        if _HAS_RIPGREP:
            display_matches, truncated, error = await _glob_with_ripgrep(
                search_root=path,
                pattern=str(pattern),
                limit=head_limit,
                offset=offset,
                exclude_globs=_denylist_ripgrep_globs(denied_patterns),
            )
            if error is not None:
                return self._error_result(error)
            output_limit_reached = False
        else:
            # Fallback glob: stop after one look-ahead result and surface the
            # 50-KiB boundary (that cap and the result ceiling mirror Pi's
            # tool-output contract; the lazy traversal shape itself is
            # MiniCode's own — Pi's fallback shells out to rg/fd instead).
            try:
                fallback = await asyncio.to_thread(
                    _glob_with_python,
                    search_root=path,
                    pattern=str(pattern),
                    limit=head_limit,
                    offset=offset,
                    is_allowed=(
                        (lambda candidate: checker.is_path_allowed(str(candidate), context=permission))
                        if checker is not None and denied_patterns
                        else None
                    ),
                )
            except SearchResourceLimitError as exc:
                return self._error_result(str(exc))
            except ValueError as exc:
                return self._error_result(str(exc))
            display_matches = fallback.matches
            truncated = fallback.truncated
            output_limit_reached = fallback.output_limit_reached

        if not display_matches:
            result = f"No files matched the pattern '{pattern}' in {directory}."
            return self._success_result(_bounded_search_output(result))

        header = f"Found {len(display_matches)} matching files for '{pattern}' in {directory}:"
        if truncated:
            header += " (more matches are available)"
        elif offset:
            header += f" (offset {offset})"

        result = header + "\n" + "\n".join("- " + m for m in display_matches)
        result += _pagination_suffix(offset=offset, head_limit=head_limit, truncated=truncated)
        if output_limit_reached:
            result += (
                "\n\n[50 KiB file-search output limit reached. "
                "Use a narrower path/pattern or a finite head_limit.]"
            )
        return self._success_result(_bounded_search_output(result))


class GrepFilesTool(BaseTool):
    """
    在目录中搜索匹配正则表达式的文件内容。

    返回匹配行的文件路径、行号和内容。默认最多 100 条结果（head_limit=0 表示不限制）。
    支持 ripgrep 后端（如已安装）和上下文行显示。
    权限: AUTO
    """

    name = "grep_files"
    read_only = True
    result_kind = "file"
    activity_kind = "workspaceSearch"
    display_label = "Search"
    description = (
        "Search file contents with ripgrep-style regex; returns matching lines with paths and line numbers by default. "
        "Use for content search instead of shell grep/rg. Supports regex, glob/type filters, output modes ('content', 'files_with_matches', 'count'), context, and multiline."
    )
    permission = PermissionLevel.AUTO
    workspace_path_fields = ("path", "directory")
    allow_workspace_root_path = True

    def __init__(self, workspace_root: Path | None = None):
        self._workspace_root = workspace_root

    def model_description(self) -> str:
        return "Regex-search file contents; returns matching paths, line numbers, and lines by default."

    def model_schema(self) -> ToolSchema:
        # A narrower model-facing schema is not a cosmetic difference here:
        # OpenAI payload normalization stamps
        # additionalProperties=false onto every object, so an omitted parameter
        # is rejected rather than ignored, and the model has to fall back to
        # run_command for context lines, globs, or a type filter.
        return ToolSchema(
            name=self.name,
            description=self.model_description(),
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regex pattern to search for."},
                    "path": {
                        "type": "string",
                        "description": "File or directory to search; defaults to workspace root.",
                    },
                    "glob": {"type": "string", "description": "Glob filter such as '**/*.py'."},
                    "type": {
                        "type": "string",
                        "description": "File type such as 'py', 'js', 'ts', 'rust', or 'go'.",
                    },
                    "output_mode": {
                        "type": "string",
                        "enum": ["content", "files_with_matches", "count"],
                        "description": (
                            "'content' shows matching lines (supports -A/-B/-C and -n), "
                            "'files_with_matches' shows paths, 'count' shows per-file counts. "
                            "Defaults to 'files_with_matches'."
                        ),
                    },
                    "-i": {"type": "boolean", "description": "Case-insensitive search."},
                    "-n": {
                        "type": "boolean",
                        "description": "Show line numbers (content mode). Default true.",
                    },
                    "-A": {
                        "type": "integer",
                        "description": "Lines of context after each match (content mode).",
                    },
                    "-B": {
                        "type": "integer",
                        "description": "Lines of context before each match (content mode).",
                    },
                    "-C": {
                        "type": "integer",
                        "description": "Lines before/after each match; when supplied, overrides -A and -B.",
                    },
                    "multiline": {
                        "type": "boolean",
                        "description": "Let the pattern span lines. Default false.",
                    },
                    "head_limit": {
                        "type": "integer",
                        "description": f"Max results. Default {GREP_MAX_MATCHES}; 0 for unlimited.",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Skip this many results; pair with head_limit to page.",
                    },
                },
                "required": ["pattern"],
            },
        )

    def get_spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            capability="workspace.grep",
            toolset="core",
            exposure="core",
            required_args=("pattern",),
        )

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Regex pattern, e.g. 'def run_agent' or 'TODO:'.",
                    },
                    "directory": {
                        "type": "string",
                        "description": "Directory to search; defaults to workspace root.",
                    },
                    "path": {
                        "type": "string",
                        "description": "Alias for directory.",
                    },
                    "file_extensions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Limit to extensions such as ['.py', '.js'].",
                    },
                    "type": {
                        "type": "string",
                        "description": "File type such as 'py', 'js', 'ts', 'rust', or 'go'.",
                    },
                    "output_mode": {
                        "type": "string",
                        "enum": ["content", "files_with_matches", "count"],
                        "description": "Output: matching lines, matching paths, or per-file counts. Default 'files_with_matches'.",
                    },
                    "case_insensitive": {
                        "type": "boolean",
                        "description": "Ignore case. Default false.",
                    },
                    "-i": {
                        "type": "boolean",
                        "description": "Alias for case_insensitive.",
                    },
                    "context": {
                        "type": "integer",
                        "description": "Context lines before/after each match. Default 0.",
                        "default": 0,
                    },
                    "-C": {
                        "type": "integer",
                        "description": "Alias for context.",
                    },
                    "-A": {
                        "type": "integer",
                        "description": "Lines of context after each match when -C/context is not supplied.",
                    },
                    "-B": {
                        "type": "integer",
                        "description": "Lines of context before each match when -C/context is not supplied.",
                    },
                    "-n": {
                        "type": "boolean",
                        "description": "Show line numbers in output (content mode). Default true.",
                    },
                    "glob": {
                        "type": "string",
                        "description": "Glob filter such as '**/*.py'.",
                    },
                    "multiline": {
                        "type": "boolean",
                        "description": "Allow patterns to span lines. Default false.",
                    },
                    "head_limit": {
                        "type": "integer",
                        "description": f"Max result lines/entries. Default {GREP_MAX_MATCHES}; 0 for unlimited.",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Skip this many results; use with head_limit for pagination.",
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
        before_context = _coerce_nonnegative_int(args.get("-B", 0))
        after_context = _coerce_nonnegative_int(args.get("-A", 0))
        if "-C" in args or "context" in args:
            before_context = 0
            after_context = 0
        line_numbers = _as_bool(args.get("-n", True), default=True)
        glob_filter = args.get("glob")
        multiline = _as_bool(args.get("multiline", False))
        head_limit = _coerce_head_limit(args.get("head_limit"), GREP_MAX_MATCHES)
        offset = _coerce_nonnegative_int(args.get("offset"), 0)

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

        denied_patterns = _denied_path_patterns(context)

        # --- Ripgrep backend (preferred whenever available) ---
        if _HAS_RIPGREP:
            rg_output, is_error = await _grep_with_ripgrep(
                pattern=pattern,
                search_root=path,
                glob_pattern=glob_filter,
                context_lines=context_lines,
                before_context=before_context,
                after_context=after_context,
                line_numbers=line_numbers,
                case_sensitive=not case_insensitive,
                limit=head_limit,
                offset=offset,
                output_mode=output_mode,
                multiline=multiline,
                file_type=file_type or None,
                file_extensions=file_extensions,
                exclude_globs=_denylist_ripgrep_globs(denied_patterns),
            )
            if is_error:
                return self._error_result(rg_output)

            header = f"在 {directory} 中搜索 '{pattern}'（模式: {output_mode}）"
            if context_lines > 0 and output_mode == "content":
                header += f"（上下文 {context_lines} 行）"
            result = header + "\n\n" + rg_output
            return self._success_result(_bounded_search_output(result))

        # --- Python fallback backend ---
        try:
            flags = re.IGNORECASE if case_insensitive else 0
            if multiline:
                flags |= re.DOTALL | re.MULTILINE
            if _safe_regex is re and _stdlib_regex_pattern_is_unsafe(pattern):
                return self._error_result(
                    "正则表达式过于复杂，当前环境无法安全执行；请改用更简单的模式"
                )
            regex = _safe_regex.compile(pattern, flags)
        except (re.error, getattr(_safe_regex, "error", re.error)) as exc:
            return self._error_result(f"无效的正则表达式: {exc}")

        checker = getattr(context, "permission_checker", None) if context is not None else None
        permission = getattr(context, "permission", None) if context is not None else None
        candidate_files: Iterator[Path] = _iter_candidate_files(
            path,
            file_extensions,
            is_allowed=(
                (lambda candidate: checker.is_path_allowed(str(candidate), context=permission))
                if checker is not None and denied_patterns
                else None
            ),
        )
        if glob_filter:
            root = path.resolve()

            def matches_glob(candidate: Path) -> bool:
                try:
                    return candidate.relative_to(root).match(str(glob_filter))
                except ValueError:
                    return False

            candidate_files = (candidate for candidate in candidate_files if matches_glob(candidate))
        try:
            batch = await asyncio.to_thread(
                _grep_candidates,
                candidate_files,
                path,
                regex,
                context_lines,
                output_mode,
                multiline,
                offset + head_limit + 1 if head_limit is not None else None,
                before_context,
                after_context,
                line_numbers,
            )
        except RegexSafetyLimitError as exc:
            return self._error_result(
                f"Regex safety limit reached: {exc}. Simplify the pattern or narrow the path."
            )
        except SearchResourceLimitError as exc:
            return self._error_result(
                f"Search resource limit reached: {exc}. Narrow the path or pattern."
            )

        display_matches, truncated = _apply_pagination(
            batch.matches,
            offset=offset,
            head_limit=head_limit,
        )

        if not display_matches:
            if batch.output_limit_reached:
                return self._error_result(
                    "Python search fallback reached the 50 KiB result boundary before "
                    "it could complete this page; narrow the pattern/path or use a smaller offset."
                )
            result = f"在 {directory} 中搜索 '{pattern}'：无匹配结果（搜索了 {batch.files_searched} 个文件）"
            return self._success_result(_bounded_search_output(result))

        header = f"在 {directory} 中搜索 '{pattern}'：找到 {len(display_matches)} 条结果（模式: {output_mode}）"
        if truncated:
            header += "（分页结果，后续仍有匹配）"
        elif offset:
            header += f"（offset {offset}）"
        if context_lines > 0 and output_mode == "content":
            header += f"（上下文 {context_lines} 行）"

        result = header + "\n\n" + "\n".join(display_matches)
        result += _pagination_suffix(offset=offset, head_limit=head_limit, truncated=truncated)
        notices: list[str] = []
        if batch.output_limit_reached:
            notices.append("50 KiB search output limit reached; refine the pattern/path")
        if batch.lines_truncated:
            notices.append(
                f"some lines were truncated to {GREP_DISPLAY_LINE_MAX_CHARS} chars; use read_file for full lines"
            )
        if batch.context_truncated:
            notices.append("context was truncated to the shared 2000-line/50-KiB tool boundary")
        if notices:
            result += f"\n\n[{'. '.join(notices)}]"
        return self._success_result(_bounded_search_output(result))