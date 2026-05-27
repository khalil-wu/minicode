"""
File operation tools.

Paths are resolved relative to the active workspace root when one exists.
Resolved paths must remain inside that workspace boundary.
"""

from __future__ import annotations

import time
import hashlib
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any

from backend.artifact.store import ArtifactStore
from backend.permissions.context import ToolExecutionContext
from backend.security.sensitive_files import is_protected_write_path, is_sensitive_file
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema
from backend.workspace.file_state_cache import get_global_file_cache

# Token budget constants.
READ_FILE_TOKEN_LIMIT = 2000  # Approximately 8000 characters.
READ_FILE_CONTEXT_PREVIEW_CHARS = 12_000
LIST_FILES_MAX_ENTRIES = 100
LIST_FILES_CACHE_TTL_SECONDS = 8.0
LIST_FILES_CACHE_MAX_ENTRIES = 128


@dataclass
class _ListFilesCacheEntry:
    expires_at: float
    directory_mtime_ns: int
    result: str


_list_files_cache: OrderedDict[str, _ListFilesCacheEntry] = OrderedDict()
_list_files_cache_lock = Lock()


def _list_files_cache_key(path: Path, recursive: bool) -> str:
    return f"{path.resolve().as_posix()}::recursive={int(bool(recursive))}"


def _get_cached_list_files_result(path: Path, recursive: bool) -> str | None:
    cache_key = _list_files_cache_key(path, recursive)
    now = time.monotonic()

    with _list_files_cache_lock:
        entry = _list_files_cache.get(cache_key)
        if entry is None:
            return None
        if now > entry.expires_at:
            _list_files_cache.pop(cache_key, None)
            return None

    try:
        current_mtime_ns = path.stat().st_mtime_ns
    except OSError:
        with _list_files_cache_lock:
            _list_files_cache.pop(cache_key, None)
        return None

    with _list_files_cache_lock:
        entry = _list_files_cache.get(cache_key)
        if entry is None:
            return None
        if current_mtime_ns != entry.directory_mtime_ns:
            _list_files_cache.pop(cache_key, None)
            return None
        _list_files_cache.move_to_end(cache_key)
        return entry.result


def _put_list_files_cache(path: Path, recursive: bool, result: str) -> None:
    try:
        directory_mtime_ns = path.stat().st_mtime_ns
    except OSError:
        return

    cache_key = _list_files_cache_key(path, recursive)
    entry = _ListFilesCacheEntry(
        expires_at=time.monotonic() + LIST_FILES_CACHE_TTL_SECONDS,
        directory_mtime_ns=directory_mtime_ns,
        result=result,
    )

    with _list_files_cache_lock:
        _list_files_cache[cache_key] = entry
        _list_files_cache.move_to_end(cache_key)
        while len(_list_files_cache) > LIST_FILES_CACHE_MAX_ENTRIES:
            _list_files_cache.popitem(last=False)


def clear_list_files_cache() -> None:
    """Clear in-memory list_files cache."""
    with _list_files_cache_lock:
        _list_files_cache.clear()


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _validate_expected_hash(path: Path, expected_hash: Any) -> tuple[bool, str]:
    if not path.exists():
        if str(expected_hash or "").strip():
            return False, "expected_hash must be empty when creating a new file"
        return True, ""

    normalized = str(expected_hash or "").strip().lower()
    if not normalized:
        return (
            False,
            "expected_hash is required for existing files. Re-read the file with read_file and retry with its content_hash.",
        )
    try:
        current_content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return False, "Only UTF-8 text files support guarded writes"
    except OSError as exc:
        return False, f"Unable to read current file for guarded write: {exc}"

    actual_hash = content_hash(current_content)
    if actual_hash != normalized:
        return (
            False,
            f"File changed on disk; expected_hash={normalized}, actual_hash={actual_hash}. Re-read before editing.",
        )
    return True, ""


class PathTraversalError(ValueError):
    """Raised when a resolved path escapes the workspace boundary."""
    pass


MAX_FILE_READ_BYTES = 10 * 1024 * 1024  # 10 MB


def _resolve_path(path_str: str, context: Any = None) -> Path:
    """
    Resolve path relative to workspace root if available.
    Validates that the resolved path stays within the workspace boundary.

    Raises:
        PathTraversalError: if the resolved path escapes workspace root.
    """
    workspace_root: Path | None = None
    if context and hasattr(context, 'workspace_root') and context.workspace_root:
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
                f"Path escapes workspace boundary: {path_str} ({workspace_root})"
            )
    else:
        # No workspace_root: restrict to CWD as a safety fallback
        cwd = Path.cwd().resolve()
        try:
            resolved.relative_to(cwd)
        except ValueError:
            raise PathTraversalError(
                f"Path escapes current working directory boundary: {path_str}"
            )

    return resolved


class ReadFileTool(BaseTool):
    """
    Read text file content.

    Small files are returned inline. Large files are saved as artifacts while
    still returning a usable preview and content hash.
    """

    name = "read_file"
    read_only = True
    description = (
        "Read text content from a file. Returns inline content for small files "
        "or an artifact reference plus preview for large files. "
        "Example: read_file(file_path='src/main.py'). "
        "Binary files and sensitive credential files are not readable."
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
                        "description": "Absolute or workspace-relative path to read.",
                    },
                    "start_line": {
                        "type": "integer",
                        "description": "Optional 1-indexed start line.",
                    },
                    "end_line": {
                        "type": "integer",
                        "description": "Optional 1-indexed inclusive end line.",
                    },
                },
                "required": ["file_path"],
            },
            strict=True,
        )

    async def execute(self, args: dict[str, Any], context: ToolExecutionContext | None = None) -> ToolResult:
        file_path = args.get("file_path", "")
        start_line = args.get("start_line")
        end_line = args.get("end_line")

        if not file_path:
            return self._error_result("Missing file_path argument")

        try:
            path = _resolve_path(file_path, context)
        except PathTraversalError as exc:
            return self._error_result(str(exc))

        if not path.exists():
            return self._error_result(f"File does not exist: {file_path}")

        if not path.is_file():
            return self._error_result(f"Not a file: {file_path}")

        if is_sensitive_file(path):
            return self._error_result(
                f"Refusing to read sensitive file: {file_path}. "
                "Open it manually or provide a redacted excerpt if it is needed."
            )
        # Refuse very large direct reads to avoid memory pressure.
        try:
            file_size = path.stat().st_size
        except OSError as exc:
            return self._error_result(f"Unable to read file metadata: {exc}")
        if file_size > MAX_FILE_READ_BYTES:
            return self._error_result(
                f"File is too large ({file_size // 1024 // 1024}MB); limit is {MAX_FILE_READ_BYTES // 1024 // 1024}MB"
            )

        # Try the file-state cache first.
        cache = get_global_file_cache()
        cached_entry = cache.get(path)

        if cached_entry is not None:
            content = cached_entry.content
        else:
            try:
                content = path.read_text(encoding="utf-8")
                # Cache file content for subsequent reads.
                language_hint = path.suffix.lstrip(".") if path.suffix else ""
                cache.put(path, content, language_hint)
            except UnicodeDecodeError:
                return self._error_result(
                    f"Cannot read binary or non-UTF-8 file: {file_path}. "
                    "This tool only supports UTF-8 text files."
                )
            except PermissionError:
                return self._error_result(f"No permission to read file: {file_path}")
            except OSError as exc:
                return self._error_result(f"Failed to read file: {exc}")

        # Apply optional line range.
        if start_line is not None or end_line is not None:
            lines = content.split("\n")
            start = max(1, start_line or 1) - 1  # Convert to 0-indexed.
            end = min(len(lines), end_line or len(lines))
            content = "\n".join(lines[start:end])

        # Store oversized content as an artifact while returning a usable preview.
        estimated_tokens = len(content) // 4
        file_hash = content_hash(content)
        if estimated_tokens <= READ_FILE_TOKEN_LIMIT:
            return self._success_result(f"{content}\n\n[content_hash: {file_hash}]")

        # Large files are saved as artifacts; the preview remains actionable.
        # The artifact can be opened later with read_artifact if needed.
        artifact_id = self._artifact_store.save(
            content=content,
            source=f"read_file({file_path})",
            type="code" if path.suffix in ('.py', '.js', '.ts', '.tsx', '.jsx', '.go', '.rs') else "text",
        )
        total_lines = len(content.split("\n"))
        preview = content[:READ_FILE_CONTEXT_PREVIEW_CHARS]
        if len(content) > READ_FILE_CONTEXT_PREVIEW_CHARS:
            preview += (
                f"\n... [truncated {len(content) - READ_FILE_CONTEXT_PREVIEW_CHARS} chars; "
                f"use read_artifact('{artifact_id}') only if the omitted tail is needed] ..."
            )

        return self._success_result(
            content=f"File {file_path} ({total_lines} lines, approx {estimated_tokens} tokens) was saved as an artifact.\ncontent_hash: {file_hash}",
            artifact_id=artifact_id,
            artifact_preview=preview,
        )


class WriteFileTool(BaseTool):
    """
    Write a complete text file.

    Existing files require an expected_hash guard before writing.
    """

    name = "write_file"
    description = (
        "Write complete UTF-8 text content to a file. "
        "Parent directories are created when needed. "
        "Use edit_file for small targeted replacements."
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
                        "description": "Path to the file to write.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Complete UTF-8 text content to write.",
                    },
                    "expected_hash": {
                        "type": "string",
                        "description": "For existing files, pass the content_hash from the latest read_file result. Use an empty string for new files.",
                    },
                },
                "required": ["file_path", "content"],
            },
            strict=True,
        )

    async def execute(self, args: dict[str, Any], context: ToolExecutionContext | None = None) -> ToolResult:
        file_path = args.get("file_path", "")
        content = args.get("content", "")

        if not file_path:
            return self._error_result("Missing file_path argument")

        try:
            path = _resolve_path(file_path, context)
        except PathTraversalError as exc:
            return self._error_result(str(exc))

        if is_sensitive_file(path):
            return self._error_result(
                f"Refusing to write sensitive file: {file_path}. "
                "Edit credential files manually outside the agent."
            )
        if is_protected_write_path(path):
            return self._error_result(
                f"Refusing to write protected path: {file_path}. "
                "Repository and agent configuration files must be edited manually."
            )

        try:
            ok, message = _validate_expected_hash(path, args.get("expected_hash"))
            if not ok:
                return self._error_result(message)
            # Symlink + parent boundary check before write
            if path.exists() and path.is_symlink():
                real_target = path.resolve()
                workspace_root = Path(context.workspace_root).resolve() if context and getattr(context, 'workspace_root', None) else Path.cwd().resolve()
                try:
                    real_target.relative_to(workspace_root)
                except ValueError:
                    return self._error_result(f"Refusing to write through symlink that escapes workspace: {file_path}")
            path.parent.mkdir(parents=True, exist_ok=True)
            parent_resolved = path.parent.resolve()
            workspace_root = Path(context.workspace_root).resolve() if context and getattr(context, 'workspace_root', None) else Path.cwd().resolve()
            try:
                parent_resolved.relative_to(workspace_root)
            except ValueError:
                return self._error_result(f"Parent directory escapes workspace boundary: {file_path}")
            path.write_text(content, encoding="utf-8")

            # Invalidate file caches after writing.
            cache = get_global_file_cache()
            cache.invalidate(path)
            clear_list_files_cache()
        except PermissionError:
            return self._error_result(f"No permission to write file: {file_path}")
        except OSError as exc:
            return self._error_result(f"Failed to write file: {exc}")

        total_lines = len(content.split("\n"))
        return self._success_result(
            f"Wrote {file_path} ({total_lines} lines, {len(content)} chars). content_hash: {content_hash(content)}"
        )


class EditFileTool(BaseTool):
    """
    Replace one exact string in a text file.

    old_string must appear exactly once.
    """

    name = "edit_file"
    description = (
        "Replace one exact string in a UTF-8 text file. "
        "old_string must be unique in the target file. "
        "Use write_file for large rewrites."
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
                        "description": "Path to the file to edit.",
                    },
                    "old_string": {
                        "type": "string",
                        "description": "Original string to replace; must appear exactly once.",
                    },
                    "new_string": {
                        "type": "string",
                        "description": "Replacement string.",
                    },
                    "expected_hash": {
                        "type": "string",
                        "description": "Required for existing files: the content_hash from the latest read_file result.",
                    },
                },
                "required": ["file_path", "old_string", "new_string"],
            },
            strict=True,
        )

    async def execute(self, args: dict[str, Any], context: ToolExecutionContext | None = None) -> ToolResult:
        file_path = args.get("file_path", "")
        old_string = args.get("old_string", "")
        new_string = args.get("new_string", "")

        if not file_path:
            return self._error_result("Missing file_path argument")
        if not old_string:
            return self._error_result("Missing old_string argument")

        try:
            path = _resolve_path(file_path, context)
        except PathTraversalError as exc:
            return self._error_result(str(exc))

        if is_sensitive_file(path):
            return self._error_result(
                f"Refusing to edit sensitive file: {file_path}. "
                "Edit credential files manually outside the agent."
            )
        if is_protected_write_path(path):
            return self._error_result(
                f"Refusing to edit protected path: {file_path}. "
                "Repository and agent configuration files must be edited manually."
            )

        if not path.exists():
            return self._error_result(f"File does not exist: {file_path}")

        ok, message = _validate_expected_hash(path, args.get("expected_hash"))
        if not ok:
            return self._error_result(message)

        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return self._error_result(f"Cannot read binary or non-UTF-8 file: {file_path}")

        # Ensure the target string is unique.
        count = content.count(old_string)
        if count == 0:
            return self._error_result(
                f"old_string was not found in {file_path}. "
                "Make sure whitespace and line endings match exactly."
            )
        if count > 1:
            return self._error_result(
                f"old_string matched {count} places in {file_path}. "
                "Provide more surrounding context so it matches exactly once."
            )

        # Perform the replacement.
        new_content = content.replace(old_string, new_string, 1)

        try:
            path.write_text(new_content, encoding="utf-8")

            # Invalidate file caches after editing.
            cache = get_global_file_cache()
            cache.invalidate(path)
            clear_list_files_cache()
        except PermissionError:
            return self._error_result(f"No permission to write file: {file_path}")

        return self._success_result(
            f"Edited {file_path}: replaced {len(old_string)} chars with {len(new_string)} chars. content_hash: {content_hash(new_content)}"
        )


class ListFilesTool(BaseTool):
    """
    List directory contents.

    Returns at most LIST_FILES_MAX_ENTRIES entries.
    """

    name = "list_files"
    read_only = True
    description = (
        "List files and child directories under a directory. "
        "Returns names and file sizes, truncated for very large directories. "
        "Example: list_files(directory='./src')."
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
                        "description": "Directory path to list.",
                    },
                    "recursive": {
                        "type": "boolean",
                        "description": "Whether to list recursively. Defaults to false.",
                    },
                },
                "required": ["directory"],
            },
        )

    async def execute(self, args: dict[str, Any], context: ToolExecutionContext | None = None) -> ToolResult:
        directory = args.get("directory", ".")
        recursive = args.get("recursive", False)

        try:
            path = _resolve_path(directory, context)
        except PathTraversalError as exc:
            return self._error_result(str(exc))

        if not path.exists():
            return self._error_result(f"Directory does not exist: {directory}")
        if not path.is_dir():
            return self._error_result(f"Not a directory: {directory}")

        cached_result = _get_cached_list_files_result(path, recursive)
        if cached_result is not None:
            return self._success_result(cached_result)

        entries: list[str] = []
        try:
            if recursive:
                for item in sorted(path.rglob("*")):
                    # Skip hidden files and __pycache__.
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
            return self._error_result(f"No permission to access directory: {directory}")

        total = len(entries)
        header = f"{directory}/ ({total} entries)"
        if total >= LIST_FILES_MAX_ENTRIES:
            header += f" [truncated at {LIST_FILES_MAX_ENTRIES} entries]"

        result = header + "\n" + "\n".join(entries)
        _put_list_files_cache(path, recursive, result)
        return self._success_result(result)


def _format_size(size: int) -> str:
    """Format a byte count for display."""
    if size < 1024:
        return f"{size} B"
    elif size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    else:
        return f"{size / (1024 * 1024):.1f} MB"
