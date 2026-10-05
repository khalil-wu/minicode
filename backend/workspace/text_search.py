from __future__ import annotations

import re
import os
import stat
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel, Field

from backend.glob_patterns import compile_glob_filter
from backend.workspace.fuzzy_search import iter_search_paths
from backend.workspace.service import WorkspaceService


class SearchBuffer(BaseModel):
    path: str
    content: str
    original: str
    content_hash: str = ""
    read_only: bool = False


class WorkspaceTextSearchRequest(BaseModel):
    query: str = Field(min_length=1)
    regex: bool = False
    case_sensitive: bool = False
    whole_word: bool = False
    include: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)
    buffers: list[SearchBuffer] = Field(default_factory=list)
    limit: int = Field(default=1000, ge=1, le=2000)


def _utf16(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _position(content: str, offset: int) -> tuple[int, int]:
    lines = re.split(r"\r\n|\r|\n", content[:offset])
    return len(lines), _utf16(lines[-1]) + 1


def search_workspace_text(service: WorkspaceService, request: WorkspaceTextSearchRequest) -> dict[str, Any]:
    expression = request.query if request.regex else re.escape(request.query)
    try:
        pattern = re.compile(expression, re.MULTILINE | (0 if request.case_sensitive else re.IGNORECASE))
    except re.error as exc:
        raise HTTPException(status_code=400, detail=f"正则表达式无效：{exc}") from exc
    includes = [compile_glob_filter(item) for item in request.include]
    excludes = [compile_glob_filter(item) for item in request.exclude]
    root = service.workspace_root_path()
    buffers: dict[str, SearchBuffer] = {}
    issues: list[dict[str, str]] = []
    def path_key(value: str) -> str:
        return value.casefold() if os.name == "nt" else value
    paths = {path_key(path.relative_to(root).as_posix()): path.relative_to(root).as_posix() for path, is_dir in iter_search_paths(root) if not is_dir}
    for buffer in request.buffers:
        try:
            path = service.resolve_workspace_path(buffer.path)
            service.ensure_editor_file_allowed(root / service.normalize_workspace_relative(buffer.path), operation="read", resolved_path=path)
        except HTTPException as exc:
            if exc.status_code not in {400, 403, 404, 422}:
                raise
            issues.append({"path": buffer.path, "message": str(exc.detail)})
            continue
        relative = path.relative_to(root).as_posix()
        buffers[path_key(relative)] = buffer
        paths[path_key(relative)] = relative
    files: list[dict[str, Any]] = []
    count = 0
    truncated = False
    for relative in sorted(paths.values()):
        if includes and not any(matches(relative) for matches in includes):
            continue
        if any(matches(relative) for matches in excludes):
            continue
        buffer = buffers.get(path_key(relative))
        if buffer is not None:
            content, original, content_hash = buffer.content, buffer.original, buffer.content_hash
            read_only = buffer.read_only
        else:
            try:
                snapshot = service.read_file(relative)
            except HTTPException as exc:
                # Binary, oversized, inaccessible or removed files cannot form
                # editable replacements. Report their actual read boundary.
                if exc.status_code not in {400, 403, 404, 413, 415, 422}:
                    raise
                issues.append({"path": relative, "message": str(exc.detail)})
                continue
            content = original = snapshot.content
            content_hash = snapshot.content_hash
            read_only = not bool((root / relative).stat().st_mode & stat.S_IWUSR)
        if "\x00" in content:
            continue
        matches: list[dict[str, Any]] = []
        for match in pattern.finditer(content):
            if request.whole_word and ((match.start() and re.match(r"\w", content[match.start() - 1]))
                or (match.end() < len(content) and re.match(r"\w", content[match.end()]))):
                continue
            if count >= request.limit:
                truncated = True
                break
            line, column = _position(content, match.start())
            end_line, end_column = _position(content, match.end())
            offset = _utf16(content[:match.start()])
            length = _utf16(match.group())
            line_start = max(content.rfind("\n", 0, match.start()), content.rfind("\r", 0, match.start())) + 1
            line_end = re.search(r"\r|\n", content[match.end():])
            snippet_end = match.end() + line_end.start() if line_end else len(content)
            matches.append({"id": f"{relative}:{offset}:{length}:{len(matches)}", "offset": offset, "length": length,
                "line": line, "column": column, "end_line": end_line, "end_column": end_column,
                "text": match.group(), "snippet": content[line_start:snippet_end],
                "groups": list(match.groups()), "named_groups": match.groupdict()})
            count += 1
        if matches:
            files.append({"path": relative, "content": content, "original": original, "content_hash": content_hash,
                "size_bytes": len(content.encode("utf-8")), "read_only": read_only, "from_buffer": buffer is not None, "matches": matches})
        if truncated:
            break
    return {"workspace_root": str(root), "files": files, "match_count": count, "truncated": truncated, "issues": issues}
