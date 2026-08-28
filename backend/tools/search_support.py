"""File grep/glob helper functions.

Extracted from ``backend/tools/search_tools.py`` so regex safety, ripgrep
transport and path filtering helpers are independent of the tool classes.
"""

from __future__ import annotations

import logging

from backend.subprocesses import (
    SubprocessOutputLimitError,
    communicate_bounded,
    spawn_exec,
)
from backend.tools.base import (
    MAX_TOOL_RESULT_BYTES,
    MAX_TOOL_RESULT_LINES,
    truncate_tool_result,
)
from backend.tools.path_resolution import (
    PathTraversalError,
    _is_declared_readable_path,
    denied_path_patterns,
)
from backend.workspace.path_filters import is_windows_reserved_path
from collections import deque
from pathlib import Path
from typing import (
    Any,
    Callable,
    Iterator,
)
import asyncio
import os
import re
import time


logger = logging.getLogger(__name__)


GREP_MAX_MATCHES = 250

REGEX_SEARCH_TIMEOUT_SECONDS = 0.75

REGEX_FILE_BUDGET_SECONDS = 5.0

RIPGREP_TIMEOUT_SECONDS = 20.0

REGEX_MAX_PATTERN_CHARS = 4096

REGEX_MAX_LINE_CHARS = 1_000_000

RIPGREP_TRANSPORT_LIMIT_BYTES = 20_000_000

PYTHON_SEARCH_TIMEOUT_SECONDS = RIPGREP_TIMEOUT_SECONDS

GREP_DISPLAY_LINE_MAX_CHARS = 500  # Pi's grep line-display limit

_BINARY_PROBE_BYTES = 8 * 1024

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


_NESTED_QUANTIFIER_RE = re.compile(
    r"\((?:[^()\\]|\\.){0,512}(?:[*+]|\{\d+(?:,\d*)?\})(?:[^()\\]|\\.){0,512}\)"
    r"\s*(?:[*+]|\{\d+(?:,\d*)?\})",
)


def _regex_pattern_is_unsafe(pattern: str) -> bool:
    """Reject the small class of catastrophic stdlib-re patterns.

    The normal ``regex`` dependency enforces a runtime timeout.  Minimal
    installations can fall back to ``re``, which has no interruptible match
    API; rejecting nested quantifiers and oversized patterns keeps that path
    fail-closed instead of allowing a ReDoS payload to pin the agent worker.
    """

    if len(pattern) > REGEX_MAX_PATTERN_CHARS:
        return True
    return bool(_NESTED_QUANTIFIER_RE.search(pattern))


def _stdlib_regex_pattern_is_unsafe(pattern: str) -> bool:
    """Fail closed when the interruptible ``regex`` engine is unavailable.

    Python's stdlib ``re`` cannot cancel a match that has entered exponential
    backtracking.  A wall-clock check after ``re.search`` is therefore not a
    safety boundary.  In the defensive no-dependency fallback, allow only
    non-repeating expressions (anchors, character classes, groups and plain
    alternation remain useful and run in bounded time) and reject backrefs.
    Normal installations use the declared ``regex`` dependency and retain the
    full syntax with engine-enforced timeouts.
    """

    if _regex_pattern_is_unsafe(pattern):
        return True
    if any(token in pattern for token in ("*", "+", "?", "{")):
        return True
    return bool(re.search(r"\\(?:[1-9]|g<|k<)", pattern))


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
            if _is_declared_readable_path(resolved, context):
                return resolved
            raise PathTraversalError(
                f"路径 {path_str} 超出工作区边界 ({workspace_root})"
            )
    elif not workspace_root and not allow_workspace_escape:
        if _is_declared_readable_path(resolved, context):
            return resolved
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
    # CC/Pi both pass --hidden; hidden source/config files are searchable.  The
    # ignored set is deliberately limited to high-noise metadata trees, while
    # sensitive-file filtering below remains an independent security boundary.
    return any(p.casefold() in _IGNORED_PATH_PARTS for p in parts) or is_windows_reserved_path(Path(*parts))


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
    """Return None for explicit unlimited; otherwise the requested limit.

    Claude Code treats ``0`` as the explicit unlimited escape hatch and does
    not silently rewrite a larger caller-supplied head_limit. The output
    budget, not an unrelated local ceiling, is the authority.
    """
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    if parsed == 0:
        return None
    return parsed if parsed > 0 else default


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


def _bounded_search_output(content: str) -> str:
    return truncate_tool_result(content)


def _normalize_output_mode(value: Any) -> str:
    # Default to files_with_matches: paths are far cheaper than every matching
    # line, and both schemas document that default.
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


def clear_search_caches() -> None:
    """Compatibility hook for the file watcher.

    Search results are intentionally not cached: materializing a complete
    candidate tree and its parent signatures defeats the bounded traversal
    model used by CC/Pi.  The watcher may still call this hook safely.
    """
    return None


def _denied_path_patterns(context: Any) -> list[str]:
    """Delegate to the shared workspace denylist projection (path_resolution)."""
    return denied_path_patterns(context)


def _denylist_ripgrep_globs(patterns: list[str]) -> list[str]:
    """Translate denylist patterns into ripgrep exclude globs."""
    globs: list[str] = []
    for pattern in patterns:
        stripped = pattern.rstrip("/")
        if not stripped:
            continue
        if pattern.endswith("/"):
            # A trailing slash denies the whole subtree, as in gitignore.
            globs.append(f"!**/{stripped}/**")
            continue
        globs.append(f"!{stripped}" if "/" in stripped else f"!**/{stripped}")
        if not stripped.endswith("**"):
            globs.append(f"!**/{stripped}/**" if "/" not in stripped else f"!{stripped}/**")
    return globs


class _BinaryFileDetected(RuntimeError):
    """Internal marker used by the bounded Python fallback."""


def _iter_candidate_files(
    path: Path,
    file_extensions: list[str],
    *,
    is_allowed: Callable[[Path], bool] | None = None,
) -> Iterator[Path]:
    """Yield eligible files without materializing the complete directory tree.

    ``os.scandir`` keeps traversal lazy and lets the caller stop as soon as a
    Pi-style match/result cap is reached.  Symlinked entries and paths outside
    the resolved root are rejected before any file is opened.
    """

    root = path.resolve()
    extension_set = {extension.casefold() for extension in file_extensions}

    def eligible(file_path: Path) -> bool:
        try:
            relative = file_path.relative_to(root)
        except ValueError:
            return False
        if _should_ignore_parts(relative.parts):
            return False
        if not _is_within_path(file_path, root):
            return False
        if extension_set and file_path.suffix.casefold() not in extension_set:
            return False
        if is_allowed is not None:
            try:
                if not is_allowed(file_path):
                    return False
            except Exception:
                # Permission evaluation failures are fail-closed for search.
                return False
        return True

    if root.is_file():
        if eligible(root):
            yield root
        return
    if not root.is_dir():
        return

    pending_dirs: list[Path] = [root]
    while pending_dirs:
        current = pending_dirs.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    candidate = Path(entry.path)
                    try:
                        if entry.is_symlink():
                            continue
                        relative = candidate.relative_to(root)
                        if _should_ignore_parts(relative.parts):
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            pending_dirs.append(candidate)
                            continue
                        if entry.is_file(follow_symlinks=False) and eligible(candidate):
                            yield candidate
                    except (OSError, ValueError):
                        continue
        except OSError:
            continue


def _iter_bounded_file_lines(
    file_path: Path,
    *,
    deadline: float,
) -> Iterator[tuple[str, bool]]:
    """Read text one bounded line at a time and probe for binary content.

    A bounded ``readline`` plus draining of an overlong line prevents a single
    generated/minified line from allocating an arbitrary amount of memory.
    ``was_truncated`` records the existing regex-input ceiling so the caller
    can tell the model that the line was intentionally shortened.
    """

    with file_path.open("rb") as handle:
        probe = handle.read(_BINARY_PROBE_BYTES)
        if b"\x00" in probe:
            raise _BinaryFileDetected
        handle.seek(0)
        while True:
            if time.monotonic() >= deadline:
                raise SearchResourceLimitError(
                    f"search exceeded the {REGEX_FILE_BUDGET_SECONDS:.2f}s per-file safety budget"
                )
            raw = handle.readline(REGEX_MAX_LINE_CHARS + 2)
            if not raw:
                return
            line_complete = raw.endswith(b"\n")
            content_bytes = raw.rstrip(b"\r\n") if line_complete else raw
            was_truncated = len(content_bytes) > REGEX_MAX_LINE_CHARS
            retained = content_bytes[:REGEX_MAX_LINE_CHARS]
            if not line_complete and len(raw) >= REGEX_MAX_LINE_CHARS + 2:
                # Drain the remainder without retaining it.  Keep probing for
                # NUL so a binary file cannot leak a match found before it.
                while True:
                    remainder = handle.readline(64 * 1024)
                    if b"\x00" in remainder:
                        raise _BinaryFileDetected
                    if not remainder or remainder.endswith(b"\n"):
                        break
            if b"\x00" in content_bytes:
                raise _BinaryFileDetected
            yield retained.decode("utf-8", errors="replace"), was_truncated


def _read_bounded_multiline_content(file_path: Path, *, deadline: float) -> str:
    try:
        stat = file_path.stat()
    except OSError:
        return ""
    if stat.st_size > RIPGREP_TRANSPORT_LIMIT_BYTES:
        raise SearchResourceLimitError(
            f"multiline fallback refuses files larger than "
            f"{RIPGREP_TRANSPORT_LIMIT_BYTES // 1_000_000} MB; narrow the path or use ripgrep"
        )
    chunks: list[bytes] = []
    retained = 0
    with file_path.open("rb") as handle:
        while True:
            if time.monotonic() >= deadline:
                raise SearchResourceLimitError(
                    f"search exceeded the {REGEX_FILE_BUDGET_SECONDS:.2f}s per-file safety budget"
                )
            chunk = handle.read(min(64 * 1024, RIPGREP_TRANSPORT_LIMIT_BYTES - retained + 1))
            if not chunk:
                break
            if b"\x00" in chunk:
                return ""
            retained += len(chunk)
            if retained > RIPGREP_TRANSPORT_LIMIT_BYTES:
                raise SearchResourceLimitError(
                    f"multiline fallback exceeded the {RIPGREP_TRANSPORT_LIMIT_BYTES // 1_000_000} MB input limit"
                )
            chunks.append(chunk)
    return b"".join(chunks).decode("utf-8", errors="replace")


def _display_search_line(line: str) -> tuple[str, bool]:
    if len(line) <= GREP_DISPLAY_LINE_MAX_CHARS:
        return line, False
    return f"{line[:GREP_DISPLAY_LINE_MAX_CHARS]}... [truncated]", True


def _format_context_block(
    rel_path: str,
    records: list[tuple[int, str, bool]],
    *,
    line_numbers: bool,
) -> tuple[str, bool]:
    rendered: list[str] = []
    encoded_size = 0
    truncated = False
    for line_number, line, is_match in records:
        if line_numbers:
            if is_match:
                text = f"  {rel_path}:{line_number}: {line}"
            else:
                text = f"  {rel_path}-{line_number}- {line}"
        else:
            text = f"  {'>' if is_match else ' '} {line}"
        line_size = len(text.encode("utf-8")) + (1 if rendered else 0)
        if rendered and (
            len(rendered) >= MAX_TOOL_RESULT_LINES
            or encoded_size + line_size > MAX_TOOL_RESULT_BYTES
        ):
            truncated = True
            break
        rendered.append(text)
        encoded_size += line_size
    return "\n".join(rendered), truncated


def _grep_file_matches(
    file_path: Path,
    root_path: Path,
    regex: Any,
    context_lines: int = 0,
    output_mode: str = "content",
    multiline: bool = False,
    before_context: int = 0,
    after_context: int = 0,
    line_numbers: bool = True,
    max_results: int | None = None,
) -> _FileGrepResult:
    if not _is_within_path(file_path, root_path):
        return _FileGrepResult([])

    file_deadline = time.monotonic() + REGEX_FILE_BUDGET_SECONDS

    # Claude Code gives explicit symmetric context (-C/context) precedence.
    # ``execute`` clears -A/-B when that parameter is present.
    if before_context > 0 or after_context > 0:
        ctx_before = before_context
        ctx_after = after_context
    else:
        ctx_before = ctx_after = context_lines
    bounded_ctx_before = min(ctx_before, MAX_TOOL_RESULT_LINES)
    bounded_ctx_after = min(ctx_after, MAX_TOOL_RESULT_LINES)
    context_truncated = (
        bounded_ctx_before != ctx_before or bounded_ctx_after != ctx_after
    )

    matches: list[str] = []
    rel_path = _relative_display_path(file_path, root_path)

    if multiline:
        content = _read_bounded_multiline_content(file_path, deadline=file_deadline)
        if not content:
            return _FileGrepResult([])
        try:
            remaining = file_deadline - time.monotonic()
            if remaining <= 0:
                raise RegexSafetyLimitError("Regex search exceeded the per-file safety budget")
            try:
                regex_matches = regex.finditer(
                    content,
                    timeout=min(REGEX_SEARCH_TIMEOUT_SECONDS, remaining),
                )
            except TypeError:
                # ``re.Pattern`` has no timeout keyword.  Unsafe nested
                # quantifiers are rejected before compilation; the file-level
                # deadline below still protects the normal bounded patterns.
                regex_matches = regex.finditer(content)
        except TimeoutError as exc:
            raise RegexSafetyLimitError(
                f"Regex search exceeded the {REGEX_SEARCH_TIMEOUT_SECONDS:.2f}s safety limit"
            ) from exc
        try:
            if output_mode == "files_with_matches":
                try:
                    next(regex_matches)
                except StopIteration:
                    return _FileGrepResult([])
                return _FileGrepResult([rel_path])
            if output_mode == "count":
                count = sum(1 for _ in regex_matches)
                if count == 0:
                    return _FileGrepResult([])
                return _FileGrepResult([f"{rel_path}:{count}"])

            output_bytes = 0
            output_lines = 0
            output_limit_reached = False
            for match in regex_matches:
                line_num = content.count("\n", 0, match.start()) + 1
                excerpt = match.group(0).strip()
                excerpt, was_truncated = _display_search_line(excerpt)
                if was_truncated:
                    context_truncated = True
                excerpt = excerpt.replace("\r\n", "\n").replace("\n", "\\n")
                locator = f"{rel_path}:{line_num}" if line_numbers else rel_path
                rendered = f"  {locator}: {excerpt}"
                matches.append(rendered)
                output_bytes += len(rendered.encode("utf-8")) + (1 if len(matches) > 1 else 0)
                output_lines += rendered.count("\n") + 1
                if output_bytes > MAX_TOOL_RESULT_BYTES or output_lines > MAX_TOOL_RESULT_LINES:
                    output_limit_reached = True
                    break
                if max_results is not None and len(matches) >= max_results:
                    break
        except TimeoutError as exc:
            raise RegexSafetyLimitError(
                f"Regex search exceeded the {REGEX_SEARCH_TIMEOUT_SECONDS:.2f}s safety limit"
            ) from exc
        return _FileGrepResult(
            matches,
            output_limit_reached=output_limit_reached,
            context_truncated=context_truncated,
        )

    try:
        line_iter = _iter_bounded_file_lines(file_path, deadline=file_deadline)
        if output_mode == "files_with_matches":
            for line, was_truncated in line_iter:
                candidate = line[:REGEX_MAX_LINE_CHARS]
                remaining = max(0.001, min(REGEX_SEARCH_TIMEOUT_SECONDS, file_deadline - time.monotonic()))
                if _regex_search(regex, candidate, timeout_seconds=remaining):
                    return _FileGrepResult(
                        [rel_path],
                        lines_truncated=was_truncated,
                        context_truncated=context_truncated,
                    )
            return _FileGrepResult([])

        if output_mode == "count":
            count = 0
            any_line_truncated = False
            for line, was_truncated in line_iter:
                any_line_truncated = any_line_truncated or was_truncated
                candidate = line[:REGEX_MAX_LINE_CHARS]
                remaining = max(0.001, min(REGEX_SEARCH_TIMEOUT_SECONDS, file_deadline - time.monotonic()))
                if _regex_search(regex, candidate, timeout_seconds=remaining):
                    count += 1
            if count == 0:
                return _FileGrepResult([])
            return _FileGrepResult(
                [f"{rel_path}:{count}"],
                lines_truncated=any_line_truncated,
                context_truncated=context_truncated,
            )

        # Content mode. A single rolling window is shared by every pending
        # match, so overlapping context does not duplicate the same source
        # lines hundreds or thousands of times.
        window: deque[tuple[int, str]] = deque(
            maxlen=max(1, bounded_ctx_before + bounded_ctx_after + 1)
        )
        pending_matches: deque[int] = deque()
        any_line_truncated = False
        output_bytes = 0
        output_lines = 0
        output_limit_reached = False

        def finish_match(match_line: int) -> None:
            nonlocal context_truncated, output_bytes, output_lines, output_limit_reached
            records = [
                (number, value, number == match_line)
                for number, value in window
                if match_line - bounded_ctx_before
                <= number
                <= match_line + bounded_ctx_after
            ]
            if not records:
                return
            if not bounded_ctx_before and not bounded_ctx_after:
                line_number, line, _is_match = records[0]
                locator = f"{rel_path}:{line_number}" if line_numbers else rel_path
                rendered = f"  {locator}: {line}"
                block_truncated = False
            else:
                rendered, block_truncated = _format_context_block(
                    rel_path,
                    records,
                    line_numbers=line_numbers,
                )
            if not rendered:
                return
            matches.append(rendered)
            output_bytes += len(rendered.encode("utf-8")) + (1 if len(matches) > 1 else 0)
            output_lines += rendered.count("\n") + 1
            output_limit_reached = output_limit_reached or (
                output_bytes > MAX_TOOL_RESULT_BYTES
                or output_lines > MAX_TOOL_RESULT_LINES
            )
            context_truncated = context_truncated or block_truncated

        for line_number, (line, was_truncated) in enumerate(line_iter, 1):
            any_line_truncated = any_line_truncated or was_truncated
            display_line, display_truncated = _display_search_line(line)
            any_line_truncated = any_line_truncated or display_truncated
            window.append((line_number, display_line))

            may_accept_match = max_results is None or (
                len(matches) + len(pending_matches) < max_results
            )
            if may_accept_match:
                candidate = line[:REGEX_MAX_LINE_CHARS]
                remaining = max(
                    0.001,
                    min(REGEX_SEARCH_TIMEOUT_SECONDS, file_deadline - time.monotonic()),
                )
                if _regex_search(regex, candidate, timeout_seconds=remaining):
                    pending_matches.append(line_number)

            while (
                pending_matches
                and line_number >= pending_matches[0] + bounded_ctx_after
            ):
                finish_match(pending_matches.popleft())
                if output_limit_reached:
                    return _FileGrepResult(
                        matches,
                        output_limit_reached=True,
                        lines_truncated=any_line_truncated,
                        context_truncated=context_truncated,
                    )

            if (
                max_results is not None
                and len(matches) >= max_results
                and not pending_matches
            ):
                break

        while pending_matches:
            finish_match(pending_matches.popleft())
            if output_limit_reached or (
                max_results is not None and len(matches) >= max_results
            ):
                break
        return _FileGrepResult(
            matches,
            output_limit_reached=output_limit_reached,
            lines_truncated=any_line_truncated,
            context_truncated=context_truncated,
        )
    except _BinaryFileDetected:
        return _FileGrepResult([])
    except (PermissionError, OSError):
        return _FileGrepResult([])


def _regex_search(regex: Any, value: str, *, timeout_seconds: float = REGEX_SEARCH_TIMEOUT_SECONDS) -> Any:
    try:
        # The third-party ``regex`` package enforces a hard timeout inside the
        # matching engine, unlike asyncio.wait_for around a worker thread.
        return regex.search(value, timeout=timeout_seconds)
    except TypeError:
        # Defensive compatibility for a stdlib ``re.Pattern`` in minimal
        # environments.  The dependency is installed in normal builds.
        try:
            return regex.search(value)
        except (RecursionError, MemoryError) as exc:
            raise RegexSafetyLimitError("Regex search exceeded the safety limit") from exc
    except TimeoutError as exc:
        raise RegexSafetyLimitError(
            f"Regex search exceeded the {REGEX_SEARCH_TIMEOUT_SECONDS:.2f}s safety limit"
        ) from exc


def _grep_candidates(
    candidate_files: Iterator[Path],
    root_path: Path,
    regex: Any,
    context_lines: int = 0,
    output_mode: str = "content",
    multiline: bool = False,
    max_matches: int | None = GREP_MAX_MATCHES,
    before_context: int = 0,
    after_context: int = 0,
    line_numbers: bool = True,
) -> _GrepBatchResult:
    matches: list[str] = []
    files_searched = 0
    output_bytes = 0
    output_lines = 0
    lines_truncated = False
    context_truncated = False
    deadline = time.monotonic() + PYTHON_SEARCH_TIMEOUT_SECONDS
    for file_path in candidate_files:
        if time.monotonic() >= deadline:
            raise SearchResourceLimitError(
                f"search exceeded the {PYTHON_SEARCH_TIMEOUT_SECONDS:.0f}s time limit"
            )
        files_searched += 1
        remaining = None if max_matches is None else max(1, max_matches - len(matches))
        file_result = _grep_file_matches(
            file_path,
            root_path,
            regex,
            context_lines,
            output_mode,
            multiline,
            before_context,
            after_context,
            line_numbers,
            remaining,
        )
        if not file_result.matches:
            continue
        if file_result.lines_truncated:
            lines_truncated = True
        if file_result.context_truncated:
            context_truncated = True
        for file_match in file_result.matches:
            encoded_size = len(file_match.encode("utf-8")) + (1 if matches else 0)
            line_count = file_match.count("\n") + 1
            matches.append(file_match)
            output_bytes += encoded_size
            output_lines += line_count
            if output_bytes > MAX_TOOL_RESULT_BYTES or output_lines > MAX_TOOL_RESULT_LINES:
                return _GrepBatchResult(
                    matches=matches,
                    files_searched=files_searched,
                    output_limit_reached=True,
                    lines_truncated=lines_truncated,
                    context_truncated=context_truncated,
                )
            if max_matches is not None and len(matches) >= max_matches:
                return _GrepBatchResult(
                    matches=matches,
                    files_searched=files_searched,
                    lines_truncated=lines_truncated,
                    context_truncated=context_truncated,
                )

    return _GrepBatchResult(
        matches=matches,
        files_searched=files_searched,
        lines_truncated=lines_truncated,
        context_truncated=context_truncated,
    )


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
    file_extensions: list[str] | None = None,
    exclude_globs: list[str] | None = None,
) -> tuple[str, bool]:
    """
    Execute grep using ripgrep (rg) binary.

    Returns (output_text, is_error).
    """
    # Search hidden files but exclude VCS metadata dirs. --no-ignore is
    # deliberately not passed, so .gitignore is still respected.
    cmd = ["rg", "--color=never", "--hidden"]
    for _vcs_dir in (".git", ".svn", ".hg", ".bzr", ".jj", ".sl"):
        cmd.extend(["--glob", f"!{_vcs_dir}"])
    cmd.extend(["--max-columns", "500"])
    if output_mode == "files_with_matches":
        cmd.extend(["--files-with-matches", "--sort=modified"])
    elif output_mode == "count":
        cmd.append("--count")
    else:
        cmd.append("--no-heading")
        # -n controls whether matched lines are prefixed with their line number.
        if line_numbers:
            cmd.append("--line-number")

    for reserved in ("nul", "con", "prn", "aux", "com[1-9]", "lpt[1-9]"):
        cmd.extend(["--iglob", f"!**/{reserved}"])
        cmd.extend(["--iglob", f"!**/{reserved}.*"])

    # Configured denylist entries too, so search and read agree on what is off
    # limits rather than search enforcing only the built-in floor.
    for exclude in exclude_globs or []:
        cmd.extend(["--glob", exclude])

    if multiline:
        cmd.extend(["-U", "--multiline-dotall"])

    if not case_sensitive:
        cmd.append("--ignore-case")

    if output_mode == "content":
        # Claude Code gives explicit symmetric context (-C/context) precedence;
        # execute() clears -A/-B when that parameter is present.
        if before_context > 0 or after_context > 0:
            if before_context > 0:
                cmd.extend(["-B", str(before_context)])
            if after_context > 0:
                cmd.extend(["-A", str(after_context)])
        elif context_lines > 0:
            cmd.extend(["-C", str(context_lines)])

    for split_pattern in _split_glob_patterns(glob_pattern):
        cmd.extend(["--glob", split_pattern])

    normalized_extensions = sorted({extension.casefold() for extension in file_extensions or []})
    if normalized_extensions:
        # Keep explicit extension filtering on ripgrep's native type-filter
        # path so it intersects with a caller's --glob instead of broadening it
        # through several positive glob overrides.
        for extension in normalized_extensions:
            cmd.extend(["--type-add", f"minicodeextensions:*{extension}"])
        cmd.extend(["--type", "minicodeextensions"])
    elif file_type:
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

    try:
        proc = await spawn_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        return f"failed to start ripgrep: {exc}", True

    try:
        stdout, stderr = await communicate_bounded(
            proc,
            timeout=RIPGREP_TIMEOUT_SECONDS,
            stdout_limit_bytes=RIPGREP_TRANSPORT_LIMIT_BYTES,
            stderr_limit_bytes=RIPGREP_TRANSPORT_LIMIT_BYTES,
        )
    except asyncio.TimeoutError:
        return (
            f"ripgrep search exceeded the {RIPGREP_TIMEOUT_SECONDS:.0f}s time limit",
            True,
        )
    except SubprocessOutputLimitError:
        return (
            "search output exceeded the 20 MB ripgrep transport limit; "
            "narrow the path/pattern or use pagination",
            True,
        )

    if proc.returncode not in (0, 1):  # 1 = no matches
        error = _decode_process_output(stderr)
        return f"ripgrep error: {error}", True

    output = _decode_process_output(stdout)
    if not output:
        output = "(no matches)"
    else:
        output_lines = [_relativize_prefixed_line(line, search_root) for line in output.splitlines()]
        if output_mode == "files_with_matches":
            # rg sorts modified time oldest-first; CC presents newest-first.
            output_lines.reverse()
        output_lines, truncated = _apply_pagination(output_lines, offset=offset, head_limit=limit)
        output = "\n".join(output_lines)
        output += _pagination_suffix(offset=offset, head_limit=limit, truncated=truncated)

    return output, False


async def _glob_with_ripgrep(
    *,
    search_root: Path,
    pattern: str,
    limit: int | None,
    offset: int,
    exclude_globs: list[str] | None = None,
) -> tuple[list[str], bool, str | None]:
    """Run CC's ripgrep-backed glob with bounded transport semantics."""

    cmd = [
        "rg",
        "--files",
        "--color=never",
        "--hidden",
        "--no-ignore",
        "--sort=modified",
        "--glob",
        pattern,
    ]
    for ignored in sorted(_IGNORED_PATH_PARTS):
        cmd.extend(["--iglob", f"!**/{ignored}/**"])
    for reserved in ("nul", "con", "prn", "aux", "com[1-9]", "lpt[1-9]"):
        cmd.extend(["--iglob", f"!**/{reserved}"])
        cmd.extend(["--iglob", f"!**/{reserved}.*"])
    for exclude in exclude_globs or []:
        cmd.extend(["--glob", exclude])
    cmd.append(str(search_root))

    try:
        proc = await spawn_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        return [], False, f"failed to start ripgrep: {exc}"
    try:
        stdout, stderr = await communicate_bounded(
            proc,
            timeout=RIPGREP_TIMEOUT_SECONDS,
            stdout_limit_bytes=RIPGREP_TRANSPORT_LIMIT_BYTES,
            stderr_limit_bytes=RIPGREP_TRANSPORT_LIMIT_BYTES,
        )
    except asyncio.TimeoutError:
        return (
            [],
            False,
            f"ripgrep file search exceeded the {RIPGREP_TIMEOUT_SECONDS:.0f}s time limit",
        )
    except SubprocessOutputLimitError:
        return (
            [],
            False,
            "file search output exceeded the 20 MB ripgrep transport limit; "
            "narrow the path/pattern or use pagination",
        )

    if proc.returncode not in (0, 1):
        error = _decode_process_output(stderr).strip()
        return [], False, f"ripgrep file search error: {error or proc.returncode}"

    all_matches = [
        _relativize_prefixed_line(line, search_root)
        for line in _decode_process_output(stdout).splitlines()
        if line.strip()
    ]
    # ``rg --sort=modified`` emits oldest-first, which is exactly Claude
    # Code's glob contract (glob.ts never reverses); keep that order.
    display_matches, truncated = _apply_pagination(
        all_matches,
        offset=offset,
        head_limit=limit,
    )
    return display_matches, truncated, None


def _glob_with_python(
    *,
    search_root: Path,
    pattern: str,
    limit: int | None,
    offset: int,
    is_allowed: Callable[[Path], bool] | None = None,
) -> _GlobFallbackResult:
    selected: list[str] = []
    skipped = 0
    selected_bytes = 0
    deadline = time.monotonic() + PYTHON_SEARCH_TIMEOUT_SECONDS
    try:
        candidates = search_root.glob(pattern)
        for file_path in candidates:
            if time.monotonic() >= deadline:
                raise SearchResourceLimitError(
                    f"file search exceeded the {PYTHON_SEARCH_TIMEOUT_SECONDS:.0f}s time limit"
                )
            try:
                if file_path.is_symlink() or not file_path.is_file():
                    continue
                relative = file_path.relative_to(search_root)
            except (OSError, ValueError):
                continue
            if _should_ignore_parts(relative.parts):
                continue
            if not _is_within_path(file_path, search_root):
                continue
            if is_allowed is not None:
                try:
                    if not is_allowed(file_path):
                        continue
                except Exception:
                    continue
            if skipped < offset:
                skipped += 1
                continue
            display_path = str(relative)
            selected.append(display_path)
            selected_bytes += len(display_path.encode("utf-8")) + 1
            if limit is not None and len(selected) > limit:
                return _GlobFallbackResult(
                    matches=selected[:limit],
                    truncated=True,
                )
            if limit is None and selected_bytes > MAX_TOOL_RESULT_BYTES:
                return _GlobFallbackResult(
                    matches=selected,
                    truncated=True,
                    output_limit_reached=True,
                )
    except (OSError, ValueError, NotImplementedError, RuntimeError) as exc:
        if isinstance(exc, SearchResourceLimitError):
            raise
        raise ValueError(f"Invalid glob pattern or error reading directory: {exc}") from exc
    return _GlobFallbackResult(matches=selected, truncated=False)



