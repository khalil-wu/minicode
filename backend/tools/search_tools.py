"""
搜索工具（DESIGN.md §8.2）。

  - grep_files: 正则搜索文件内容。默认 ≤250 条匹配行（0=unlimited）。权限: AUTO
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

from backend.agent.cache_metrics import args_signature, emit_cache_metric
from backend.permissions.context import ToolExecutionContext
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema
from backend.tools.contracts import ToolSpec
from backend.tools.path_resolution import PathTraversalError
from backend.workspace.path_filters import is_windows_reserved_path

GREP_MAX_MATCHES = 250
GLOB_MAX_MATCHES = 100
SEARCH_MAX_EXPLICIT_HEAD_LIMIT = 1000
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


def _coerce_head_limit(value: Any, default: int) -> int | None:
    """Return None for explicit unlimited; otherwise a bounded positive limit."""
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    if parsed == 0:
        return None
    return max(1, min(parsed, SEARCH_MAX_EXPLICIT_HEAD_LIMIT))


def _apply_pagination(items: list[str], *, offset: int, head_limit: int | None) -> tuple[list[str], bool]:
    start = max(0, offset)
    if head_limit is None:
        return items[start:], start > 0 and bool(items[:start])
    end = start + head_limit
    return items[start:end], len(items) > end


def _pagination_suffix(*, offset: int, head_limit: int | None, truncated: bool) -> str:
    parts: list[str] = []
    if head_limit is not None and truncated:
        parts.append(f"head_limit={head_limit}")
    if offset:
        parts.append(f"offset={offset}")
    if not parts:
        return ""
    return f"\n\n[Showing paginated results: {', '.join(parts)}. Use offset to fetch the next page.]"


def _normalize_output_mode(value: Any) -> str:
    mode = str(value or "files_with_matches").strip()
    return mode if mode in _GREP_OUTPUT_MODES else "files_with_matches"


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
    result, _stale = _search_cache_lookup(cache, cache_key)
    return result


def _search_cache_lookup(
    cache: OrderedDict[str, tuple[float, str]],
    cache_key: str,
) -> tuple[str | None, bool]:
    now = time.monotonic()
    with _search_cache_lock:
        entry = cache.get(cache_key)
        if entry is None:
            return None, False

        expires_at, cached_result = entry
        if now > expires_at:
            cache.pop(cache_key, None)
            return None, True

        cache.move_to_end(cache_key)
        return cached_result, False


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
    before_context: int = 0,
    after_context: int = 0,
    line_numbers: bool = True,
) -> list[str]:
    if not _is_within_path(file_path, root_path):
        return []
    try:
        content = file_path.read_text(encoding="utf-8", errors="ignore")
    except (PermissionError, OSError):
        return []

    # Asymmetric -A/-B take precedence over symmetric -C when supplied.
    if before_context > 0 or after_context > 0:
        ctx_before = before_context
        ctx_after = after_context
    else:
        ctx_before = ctx_after = context_lines

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
            locator = f"{rel_path}:{line_num}" if line_numbers else rel_path
            matches.append(f"  {locator}: {excerpt}")
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
            if ctx_before > 0 or ctx_after > 0:
                ctx_start = max(0, line_num - 1 - ctx_before)
                ctx_end = min(len(lines), line_num + ctx_after)
                block: list[str] = []
                for i in range(ctx_start, ctx_end):
                    prefix = ">" if i == line_num - 1 else " "
                    if line_numbers:
                        block.append(f"  {prefix} {i + 1}: {lines[i].rstrip()}")
                    else:
                        block.append(f"  {prefix} {lines[i].rstrip()}")
                matches.append(f"  {rel_path}:\n" + "\n".join(block))
            else:
                locator = f"{rel_path}:{line_num}" if line_numbers else rel_path
                matches.append(f"  {locator}: {line.rstrip()}")
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
    max_matches: int | None = GREP_MAX_MATCHES,
    before_context: int = 0,
    after_context: int = 0,
    line_numbers: bool = True,
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
                before_context,
                after_context,
                line_numbers,
            )
            if not file_matches:
                continue
            if max_matches is None:
                matches.extend(file_matches)
                continue
            remaining = max_matches - len(matches)
            matches.extend(file_matches[:remaining])
            if len(matches) >= max_matches:
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
        before_context=before_context,
        after_context=after_context,
        line_numbers=line_numbers,
    )
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for file_matches in executor.map(worker, candidate_files, chunksize=8):
            if not file_matches:
                continue
            if max_matches is None:
                matches.extend(file_matches)
                continue
            remaining = max_matches - len(matches)
            matches.extend(file_matches[:remaining])
            if len(matches) >= max_matches:
                break

    return matches, files_searched


async def _grep_with_ripgrep(
    pattern: str,
    search_root: Path,
    glob_pattern: str | None = None,
    context_lines: int = 0,
    before_context: int = 0,
    after_context: int = 0,
    line_numbers: bool = True,
    case_sensitive: bool = True,
    limit: int | None = GREP_MAX_MATCHES,
    offset: int = 0,
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
        cmd.append("--no-heading")
        # -n controls whether matched lines are prefixed with their line number.
        if line_numbers:
            cmd.append("--line-number")

    for reserved in ("nul", "con", "prn", "aux", "com[1-9]", "lpt[1-9]"):
        cmd.extend(["--glob", f"!**/{reserved}"])
        cmd.extend(["--glob", f"!**/{reserved}.*"])

    if multiline:
        cmd.extend(["-U", "--multiline-dotall"])

    if not case_sensitive:
        cmd.append("--ignore-case")

    if output_mode == "content":
        # Asymmetric -A/-B take precedence over symmetric -C when supplied.
        if before_context > 0 or after_context > 0:
            if before_context > 0:
                cmd.extend(["-B", str(before_context)])
            if after_context > 0:
                cmd.extend(["-A", str(after_context)])
        elif context_lines > 0:
            cmd.extend(["-C", str(context_lines)])

    for split_pattern in _split_glob_patterns(glob_pattern):
        cmd.extend(["--glob", split_pattern])

    if file_type:
        cmd.extend(["--type", file_type])

    # NOTE: do NOT pass rg --max-count here. --max-count caps matches *per file*,
    # which combined with output-side offset pagination silently dropped results
    # (offset>0 skipped past a per-file-capped set). Pagination is applied over
    # the full output below so offset never loses data.
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
        output_lines, truncated = _apply_pagination(output_lines, offset=offset, head_limit=limit)
        output = "\n".join(output_lines)
        output += _pagination_suffix(offset=offset, head_limit=limit, truncated=truncated)

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
    result_kind = "file"
    activity_kind = "workspaceSearch"
    display_label = "Search"
    panel_hint = "inspector"
    description = (
        "Fast file-name glob search across the workspace; returns paths sorted by modification time. "
        "Use for name patterns like '**/*.py' or 'src/**/*.ts'. For content search, use grep_files."
    )
    permission = PermissionLevel.AUTO

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
                    "pattern": {"type": "string"},
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
            arg_roles={"pattern": "search_query"},
            repair_policy={"pattern": "resource_resolver"},
            empty_args_policy="repair_or_block",
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

        cache_key = _build_cache_key(
            "glob",
            {
                    "directory": str(path),
                    "pattern": pattern,
                    "head_limit": head_limit,
                    "offset": offset,
                },
            )
        cached_result, stale_cache = _search_cache_lookup(_glob_result_cache, cache_key)
        signature = args_signature(cache_key)
        if cached_result is not None:
            await emit_cache_metric(
                context,
                cache_layer="glob_files.search",
                tool_name=self.name,
                args_signature_value=signature,
                hit=True,
                payload_size_bytes=len(cached_result.encode("utf-8")),
            )
            return self._success_result(cached_result)

        matches: list[tuple[str, float]] = []
        traversed = 0
        traversal_truncated = False
        max_traversal = 50_000

        # Perform the glob matching
        try:
            for file_path in path.glob(pattern):
                traversed += 1
                if traversed > max_traversal:
                    traversal_truncated = True
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
            if traversal_truncated:
                result += (
                    f"\n\n[Results incomplete / truncated: traversal stopped after "
                    f"scanning {max_traversal} entries. Use a more specific path or pattern.]"
                )
            _search_cache_put(_glob_result_cache, cache_key, result)
            await emit_cache_metric(
                context,
                cache_layer="glob_files.search",
                tool_name=self.name,
                args_signature_value=signature,
                hit=False,
                stale=stale_cache,
                payload_size_bytes=len(result.encode("utf-8")),
            )
            return self._success_result(result)

        matches.sort(key=lambda item: (-item[1], item[0]))
        total_matches = len(matches)
        all_matches = [match for match, _modified_at in matches]
        display_matches, truncated = _apply_pagination(all_matches, offset=offset, head_limit=head_limit)
        header = f"Found {len(display_matches)} matching files for '{pattern}' in {directory}:"
        if truncated:
            header += f" (showing a page out of {total_matches})"
        elif offset:
            header += f" (offset {offset}, total {total_matches})"

        result = header + "\n" + "\n".join("- " + m for m in display_matches)
        result += _pagination_suffix(offset=offset, head_limit=head_limit, truncated=truncated)
        if traversal_truncated:
            result += (
                f"\n\n[Results incomplete / truncated: traversal stopped after scanning "
                f"{max_traversal} entries, so some matches may be missing. Use a more "
                f"specific path or pattern.]"
            )
        _search_cache_put(_glob_result_cache, cache_key, result)
        await emit_cache_metric(
            context,
            cache_layer="glob_files.search",
            tool_name=self.name,
            args_signature_value=signature,
            hit=False,
            stale=stale_cache,
            payload_size_bytes=len(result.encode("utf-8")),
        )
        return self._success_result(result)


class GrepFilesTool(BaseTool):
    """
    在目录中搜索匹配正则表达式的文件内容。

    返回匹配行的文件路径、行号和内容。默认最多 250 条结果（head_limit=0 表示不限制）。
    支持 ripgrep 后端（如已安装）和上下文行显示。
    权限: AUTO
    """

    name = "grep_files"
    read_only = True
    result_kind = "file"
    activity_kind = "workspaceSearch"
    display_label = "Search"
    panel_hint = "inspector"
    description = (
        "Search file contents with ripgrep-style regex; returns matching lines with paths and line numbers by default. "
        "Use for content search instead of shell grep/rg. Supports regex, glob/type filters, output modes ('content', 'files_with_matches', 'count'), context, and multiline."
    )
    permission = PermissionLevel.AUTO

    def __init__(self, workspace_root: Path | None = None):
        self._workspace_root = workspace_root

    def model_description(self) -> str:
        return "Regex-search file contents and return matching lines, files, or counts."

    def model_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.model_description(),
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "directory": {"type": "string"},
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
            arg_roles={"pattern": "search_query"},
            repair_policy={"pattern": "resource_resolver"},
            empty_args_policy="repair_or_block",
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
                        "description": "Lines of context to show after each match (content mode). Overrides -C.",
                    },
                    "-B": {
                        "type": "integer",
                        "description": "Lines of context to show before each match (content mode). Overrides -C.",
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
                    "before_context": int(before_context),
                    "after_context": int(after_context),
                    "line_numbers": bool(line_numbers),
                    "multiline": bool(multiline),
                    "head_limit": head_limit,
                    "offset": offset,
                    "backend": "ripgrep",
                },
            )
            cached_result, stale_cache = _search_cache_lookup(_grep_result_cache, cache_key)
            signature = args_signature(cache_key)
            if cached_result is not None:
                await emit_cache_metric(
                    context,
                    cache_layer="grep_files.search",
                    tool_name=self.name,
                    args_signature_value=signature,
                    hit=True,
                    payload_size_bytes=len(cached_result.encode("utf-8")),
                )
                return self._success_result(cached_result)

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
            )
            if is_error:
                return self._error_result(rg_output)

            header = f"在 {directory} 中搜索 '{pattern}'（模式: {output_mode}）"
            if context_lines > 0 and output_mode == "content":
                header += f"（上下文 {context_lines} 行）"
            result = header + "\n\n" + rg_output
            _search_cache_put(_grep_result_cache, cache_key, result)
            await emit_cache_metric(
                context,
                cache_layer="grep_files.search",
                tool_name=self.name,
                args_signature_value=signature,
                hit=False,
                stale=stale_cache,
                payload_size_bytes=len(result.encode("utf-8")),
            )
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
                "before_context": int(before_context),
                "after_context": int(after_context),
                "line_numbers": bool(line_numbers),
                "glob": glob_filter or "",
                "output_mode": output_mode,
                "multiline": bool(multiline),
                "head_limit": head_limit,
                "offset": offset,
                "backend": "python",
            },
        )
        cached_result, stale_cache = _search_cache_lookup(_grep_result_cache, cache_key)
        signature = args_signature(cache_key)
        if cached_result is not None:
            await emit_cache_metric(
                context,
                cache_layer="grep_files.search",
                tool_name=self.name,
                args_signature_value=signature,
                hit=True,
                payload_size_bytes=len(cached_result.encode("utf-8")),
            )
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
            offset + head_limit + 1 if head_limit is not None else None,
            before_context,
            after_context,
            line_numbers,
        )

        display_matches, truncated = _apply_pagination(matches, offset=offset, head_limit=head_limit)

        if not display_matches:
            result = f"在 {directory} 中搜索 '{pattern}'：无匹配结果（搜索了 {files_searched} 个文件）"
            _search_cache_put(_grep_result_cache, cache_key, result)
            await emit_cache_metric(
                context,
                cache_layer="grep_files.search",
                tool_name=self.name,
                args_signature_value=signature,
                hit=False,
                stale=stale_cache,
                payload_size_bytes=len(result.encode("utf-8")),
            )
            return self._success_result(result)

        header = f"在 {directory} 中搜索 '{pattern}'：找到 {len(display_matches)} 条结果（模式: {output_mode}）"
        if truncated:
            header += "（分页结果，后续仍有匹配）"
        elif offset:
            header += f"（offset {offset}）"
        if context_lines > 0 and output_mode == "content":
            header += f"（上下文 {context_lines} 行）"

        result = header + "\n\n" + "\n".join(display_matches)
        result += _pagination_suffix(offset=offset, head_limit=head_limit, truncated=truncated)
        _search_cache_put(_grep_result_cache, cache_key, result)
        await emit_cache_metric(
            context,
            cache_layer="grep_files.search",
            tool_name=self.name,
            args_signature_value=signature,
            hit=False,
            stale=stale_cache,
            payload_size_bytes=len(result.encode("utf-8")),
        )
        return self._success_result(result)
