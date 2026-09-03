"""Local MiniCode memory backend for list/read/search/ad-hoc-note tools."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.atomic_io import atomic_write_text, file_mutation_locks
from backend.memory.text_utils import truncate_middle_tokens as _truncate_middle_tokens


DEFAULT_LIST_MAX_RESULTS = 2_000
MAX_LIST_RESULTS = 2_000
DEFAULT_SEARCH_MAX_RESULTS = 200
MAX_SEARCH_RESULTS = 200
DEFAULT_READ_MAX_TOKENS = 20_000
_AD_HOC_FILENAME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-[a-z0-9][a-z0-9-]{0,79}\.md$"
)


class MemoryBackendError(RuntimeError):
    pass


@dataclass(frozen=True)
class _SearchMode:
    kind: str
    line_count: int = 0


class LocalMemoryBackend:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).expanduser().resolve()

    @staticmethod
    def _reject_symlink(path: Path, display: str) -> None:
        if path.is_symlink():
            raise MemoryBackendError(f"path '{display}' must not be a symlink")

    def resolve(self, relative_path: str | None = None) -> Path:
        if relative_path is None or not str(relative_path):
            return self.root
        raw = str(relative_path)
        relative = Path(raw)
        if relative.is_absolute() or any(part == ".." for part in relative.parts):
            raise MemoryBackendError(f"path '{raw}' must stay within the memories root")
        if any(part.startswith(".") for part in relative.parts):
            raise MemoryBackendError(f"path '{raw}' was not found")

        current = self.root
        self._reject_symlink(current, "")
        for index, part in enumerate(relative.parts):
            current = current / part
            if not current.exists() and not current.is_symlink():
                current = current.joinpath(*relative.parts[index + 1 :])
                break
            self._reject_symlink(current, current.relative_to(self.root).as_posix())
            if index + 1 < len(relative.parts) and not current.is_dir():
                raise MemoryBackendError(
                    f"path '{raw}' traverses through a non-directory path component"
                )
        return current

    def list(
        self,
        *,
        path: str | None = None,
        cursor: str | None = None,
        max_results: int | None = None,
    ) -> dict[str, Any]:
        start = self.resolve(path)
        if not start.exists():
            raise MemoryBackendError(f"path '{path or ''}' was not found")
        self._reject_symlink(start, path or "")
        try:
            start_index = int(cursor or 0)
        except ValueError as exc:
            raise MemoryBackendError(
                f"cursor '{cursor}' must be a non-negative integer"
            ) from exc
        if start_index < 0:
            raise MemoryBackendError(f"cursor '{cursor}' must be a non-negative integer")

        if start.is_file():
            entries = [{"path": start.relative_to(self.root).as_posix(), "entry_type": "file"}]
        elif start.is_dir():
            entries = []
            for candidate in sorted(start.iterdir()):
                if candidate.name.startswith(".") or candidate.is_symlink():
                    continue
                if candidate.is_dir():
                    entry_type = "directory"
                elif candidate.is_file():
                    entry_type = "file"
                else:
                    continue
                entries.append(
                    {
                        "path": candidate.relative_to(self.root).as_posix(),
                        "entry_type": entry_type,
                    }
                )
        else:
            entries = []
        if start_index > len(entries):
            raise MemoryBackendError(f"cursor '{start_index}' exceeds result count")
        requested_limit = DEFAULT_LIST_MAX_RESULTS if max_results is None else int(max_results)
        limit = min(MAX_LIST_RESULTS, max(1, requested_limit))
        end_index = min(len(entries), start_index + limit)
        next_cursor = str(end_index) if end_index < len(entries) else None
        return {
            "path": path,
            "entries": entries[start_index:end_index],
            "next_cursor": next_cursor,
            "truncated": next_cursor is not None,
        }

    def read(
        self,
        *,
        path: str,
        line_offset: int = 1,
        max_lines: int | None = None,
    ) -> dict[str, Any]:
        if line_offset < 1:
            raise MemoryBackendError("line_offset must be a 1-indexed line number")
        if max_lines is not None and max_lines < 1:
            raise MemoryBackendError("max_lines must be a positive integer")
        target = self.resolve(path)
        if not target.exists():
            raise MemoryBackendError(f"path '{path}' was not found")
        self._reject_symlink(target, path)
        if not target.is_file():
            raise MemoryBackendError(f"path '{path}' is not a file")
        try:
            original = target.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise MemoryBackendError(f"I/O error while reading memories: {exc}") from exc
        if line_offset == 1:
            start_index = 0
        else:
            current_line = 1
            start_index = -1
            for index, char in enumerate(original):
                if char == "\n":
                    current_line += 1
                    if current_line == line_offset:
                        start_index = index + 1
                        break
            if start_index < 0:
                raise MemoryBackendError("line_offset exceeds file length")
        end_index = len(original)
        if max_lines is not None:
            lines_seen = 1
            for index in range(start_index, len(original)):
                if original[index] == "\n":
                    if lines_seen == max_lines:
                        end_index = index + 1
                        break
                    lines_seen += 1
        content_from_offset = original[start_index:end_index]
        content = _truncate_middle_tokens(content_from_offset, DEFAULT_READ_MAX_TOKENS)
        return {
            "path": path,
            "start_line_number": line_offset,
            "content": content,
            "truncated": end_index < len(original) or content != content_from_offset,
        }

    @staticmethod
    def _search_mode(raw: Any) -> _SearchMode:
        if raw is None:
            return _SearchMode("any")
        if isinstance(raw, str):
            kind = raw.strip().lower()
            if kind in {"any", "all_on_same_line"}:
                return _SearchMode(kind)
        if isinstance(raw, dict):
            kind = str(raw.get("type") or "").strip().lower()
            if kind in {"any", "all_on_same_line"}:
                return _SearchMode(kind)
            if kind == "all_within_lines":
                line_count = int(raw.get("line_count") or 0)
                if line_count > 0:
                    return _SearchMode(kind, line_count)
                raise MemoryBackendError(
                    "all_within_lines.line_count must be a positive integer"
                )
        raise MemoryBackendError("invalid memory search match_mode")

    @staticmethod
    def _prepare_search_value(value: str, *, case_sensitive: bool, normalized: bool) -> str:
        prepared = value if case_sensitive else value.lower()
        return "".join(char for char in prepared if char.isalnum()) if normalized else prepared

    def search(
        self,
        *,
        queries: list[str],
        match_mode: Any = None,
        path: str | None = None,
        cursor: str | None = None,
        context_lines: int = 0,
        case_sensitive: bool = True,
        normalized: bool = False,
        max_results: int | None = None,
    ) -> dict[str, Any]:
        clean_queries = [str(query).strip() for query in queries]
        if not clean_queries or any(not query for query in clean_queries):
            raise MemoryBackendError("queries must not be empty or contain empty strings")
        mode = self._search_mode(match_mode)
        start = self.resolve(path)
        if not start.exists():
            raise MemoryBackendError(f"path '{path or ''}' was not found")
        self._reject_symlink(start, path or "")
        try:
            start_index = int(cursor or 0)
        except ValueError as exc:
            raise MemoryBackendError(
                f"cursor '{cursor}' must be a non-negative integer"
            ) from exc
        if start_index < 0:
            raise MemoryBackendError(f"cursor '{cursor}' must be a non-negative integer")
        context_lines = int(context_lines)
        if context_lines < 0:
            raise MemoryBackendError("context_lines must be a non-negative integer")
        prepared_queries = [
            self._prepare_search_value(
                query,
                case_sensitive=case_sensitive,
                normalized=normalized,
            )
            for query in clean_queries
        ]
        if any(not query for query in prepared_queries):
            raise MemoryBackendError("queries must not be empty or contain empty strings")

        files: list[Path] = []
        if start.is_file():
            files = [start]
        elif start.is_dir():
            for current, dir_names, file_names in os.walk(start, followlinks=False):
                current_path = Path(current)
                dir_names[:] = sorted(
                    name
                    for name in dir_names
                    if not name.startswith(".") and not (current_path / name).is_symlink()
                )
                files.extend(
                    current_path / name
                    for name in sorted(file_names)
                    if not name.startswith(".") and not (current_path / name).is_symlink()
                )

        matches: list[dict[str, Any]] = []
        for candidate in files:
            try:
                lines = candidate.read_text(encoding="utf-8").splitlines()
            except UnicodeDecodeError:
                continue
            flags = [
                [
                    query in self._prepare_search_value(
                        line,
                        case_sensitive=case_sensitive,
                        normalized=normalized,
                    )
                    for query in prepared_queries
                ]
                for line in lines
            ]
            windows: list[tuple[int, int, list[bool]]] = []
            if mode.kind == "any":
                windows = [
                    (index, index, line_flags)
                    for index, line_flags in enumerate(flags)
                    if any(line_flags)
                ]
            elif mode.kind == "all_on_same_line":
                windows = [
                    (index, index, line_flags)
                    for index, line_flags in enumerate(flags)
                    if all(line_flags)
                ]
            else:
                for begin, begin_flags in enumerate(flags):
                    if not any(begin_flags):
                        continue
                    aggregate = [False] * len(prepared_queries)
                    for end in range(begin, min(len(lines), begin + mode.line_count)):
                        aggregate = [
                            previous or current
                            for previous, current in zip(aggregate, flags[end])
                        ]
                        if all(aggregate):
                            windows.append((begin, end, list(aggregate)))
                            break
                windows = [
                    window
                    for index, window in enumerate(windows)
                    if not any(
                        index != other_index
                        and window[0] <= other[0]
                        and window[1] >= other[1]
                        and (window[0], window[1]) != (other[0], other[1])
                        for other_index, other in enumerate(windows)
                    )
                ]
            for begin, end, matched_flags in windows:
                content_begin = max(0, begin - context_lines)
                content_end = min(len(lines), end + context_lines + 1)
                matches.append(
                    {
                        "path": candidate.relative_to(self.root).as_posix(),
                        "match_line_number": begin + 1,
                        "content_start_line_number": content_begin + 1,
                        "content": "\n".join(lines[content_begin:content_end]),
                        "matched_queries": [
                            query
                            for query, matched in zip(clean_queries, matched_flags)
                            if matched
                        ],
                    }
                )
        matches.sort(key=lambda item: (item["path"], item["match_line_number"]))
        if start_index > len(matches):
            raise MemoryBackendError(f"cursor '{start_index}' exceeds result count")
        requested_limit = DEFAULT_SEARCH_MAX_RESULTS if max_results is None else int(max_results)
        limit = min(MAX_SEARCH_RESULTS, max(1, requested_limit))
        end_index = min(len(matches), start_index + limit)
        next_cursor = str(end_index) if end_index < len(matches) else None
        return {
            "queries": clean_queries,
            "match_mode": (
                {"type": mode.kind, "line_count": mode.line_count}
                if mode.kind == "all_within_lines"
                else {"type": mode.kind}
            ),
            "path": path,
            "matches": matches[start_index:end_index],
            "next_cursor": next_cursor,
            "truncated": next_cursor is not None,
        }

    def add_ad_hoc_note(self, *, filename: str, note: str) -> dict[str, Any]:
        encoded_name = str(filename).encode("utf-8")
        if len(encoded_name) > 128:
            raise MemoryBackendError(
                f"filename '{filename}' must be at most 128 bytes"
            )
        if not _AD_HOC_FILENAME_RE.fullmatch(str(filename)):
            raise MemoryBackendError(
                f"filename '{filename}' must use YYYY-MM-DDTHH-MM-SS-<slug>.md"
            )
        if not str(note).strip():
            raise MemoryBackendError("ad-hoc note must not be empty")

        notes_dir = self.root / "extensions" / "ad_hoc" / "notes"
        target = notes_dir / filename
        try:
            with file_mutation_locks([self.root, notes_dir, target]):
                current = self.root
                for part in ("extensions", "ad_hoc", "notes"):
                    self._reject_symlink(current, current.relative_to(self.root).as_posix())
                    current = current / part
                    if current.exists() or current.is_symlink():
                        self._reject_symlink(current, current.relative_to(self.root).as_posix())
                        if not current.is_dir():
                            raise MemoryBackendError(f"path '{current}' must be a directory")
                    else:
                        current.mkdir(parents=True, exist_ok=False)
                atomic_write_text(target, note, overwrite=False)
        except FileExistsError as exc:
            raise MemoryBackendError(f"ad-hoc note '{filename}' already exists") from exc
        return {}
