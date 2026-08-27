"""MiniCode project memory management tools."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any, Callable

from filelock import Timeout as FileLockTimeout

from backend.memory.file_memory import FileMemory
from backend.memory.local_backend import LocalMemoryBackend, MemoryBackendError
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema

if TYPE_CHECKING:
    from backend.permissions.context import ToolExecutionContext


class _MemoryTool(BaseTool):
    result_kind = "memory"
    permission = PermissionLevel.AUTO

    def __init__(self, memory: FileMemory) -> None:
        self._memory = memory

    def _memory_for(self, context: ToolExecutionContext | None) -> FileMemory:
        workspace_root = getattr(context, "workspace_root", None) if context else None
        return FileMemory.for_workspace(workspace_root) if workspace_root else self._memory

    def _execute_backend_sync(
        self,
        context: ToolExecutionContext | None,
        operation: Callable[[LocalMemoryBackend], dict[str, Any]],
        *,
        lock: bool = False,
    ) -> ToolResult:
        memory = self._memory_for(context)
        try:
            if lock:
                with memory.reset_lock.acquire(timeout=5.0):
                    payload = operation(LocalMemoryBackend(memory.memory_dir))
            else:
                payload = operation(LocalMemoryBackend(memory.memory_dir))
        except (MemoryBackendError, ValueError, TypeError) as exc:
            return self._error_result(str(exc))
        except FileLockTimeout:
            return self._error_result("timed out waiting for the memory workspace lock")
        except OSError as exc:
            return self._error_result(f"I/O error while reading memories: {exc}")
        return self._success_result(json.dumps(payload, ensure_ascii=False))

    async def _execute_backend(
        self,
        context: ToolExecutionContext | None,
        operation: Callable[[LocalMemoryBackend], dict[str, Any]],
        *,
        lock: bool = False,
    ) -> ToolResult:
        """Run file-backed memory I/O off the shared agent event loop.

        MiniCode's memory tools are ordinary asynchronous tool executions.  The
        local implementation may construct a ``FileLock`` and walk/read a
        project tree, so keeping that work in the coroutine would stall every
        WebSocket conversation while another process holds the reset lock.
        """
        return await asyncio.to_thread(
            self._execute_backend_sync,
            context,
            operation,
            lock=lock,
        )


class MemoryListTool(_MemoryTool):
    name = "memory_list"
    display_label = "List memories"
    activity_kind = "fileRead"
    read_only = True
    description = "List immediate files and directories under a path in the MiniCode memories store."

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "path": {"type": "string"},
                    "cursor": {"type": "string"},
                    "max_results": {"type": "integer", "minimum": 1},
                },
            },
        )

    async def execute(self, args: dict[str, Any], context: ToolExecutionContext | None = None) -> ToolResult:
        return await self._execute_backend(
            context,
            lambda backend: backend.list(
                path=args.get("path"),
                cursor=args.get("cursor"),
                max_results=args.get("max_results"),
            ),
        )


class MemoryReadTool(_MemoryTool):
    name = "memory_read"
    display_label = "Read memory"
    activity_kind = "fileRead"
    read_only = True
    description = (
        "Read a MiniCode memory file by relative path, optionally starting at a "
        "1-indexed line offset and limiting the number of lines returned."
    )

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "additionalProperties": False,
                "required": ["path"],
                "properties": {
                    "path": {"type": "string"},
                    "line_offset": {"type": "integer", "minimum": 1},
                    "max_lines": {"type": "integer", "minimum": 1},
                },
            },
        )

    async def execute(self, args: dict[str, Any], context: ToolExecutionContext | None = None) -> ToolResult:
        path = args.get("path")
        if not isinstance(path, str):
            return self._error_result("path is required")
        return await self._execute_backend(
            context,
            lambda backend: backend.read(
                path=path,
                line_offset=args.get("line_offset", 1),
                max_lines=args.get("max_lines"),
            ),
        )


class MemorySearchTool(_MemoryTool):
    name = "memory_search"
    display_label = "Search memories"
    activity_kind = "search"
    read_only = True
    description = (
        "Search MiniCode memory files for substring matches, optionally normalizing "
        "separators or requiring all query substrings on the same line or within a line window."
    )

    def get_schema(self) -> ToolSchema:
        match_mode = {
            "oneOf": [
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["type"],
                    "properties": {"type": {"type": "string", "enum": ["any", "all_on_same_line"]}},
                },
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["type", "line_count"],
                    "properties": {
                        "type": {"type": "string", "enum": ["all_within_lines"]},
                        "line_count": {"type": "integer", "minimum": 1},
                    },
                },
            ]
        }
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "additionalProperties": False,
                "required": ["queries"],
                "properties": {
                    "queries": {"type": "array", "minItems": 1, "items": {"type": "string"}},
                    "match_mode": match_mode,
                    "path": {"type": "string"},
                    "cursor": {"type": "string"},
                    "context_lines": {"type": "integer", "minimum": 0},
                    "case_sensitive": {"type": "boolean"},
                    "normalized": {"type": "boolean"},
                    "max_results": {"type": "integer", "minimum": 1},
                },
            },
        )

    async def execute(self, args: dict[str, Any], context: ToolExecutionContext | None = None) -> ToolResult:
        queries = args.get("queries")
        if not isinstance(queries, list):
            return self._error_result("queries is required")
        return await self._execute_backend(
            context,
            lambda backend: backend.search(
                queries=queries,
                match_mode=args.get("match_mode"),
                path=args.get("path"),
                cursor=args.get("cursor"),
                context_lines=args.get("context_lines", 0),
                case_sensitive=args.get("case_sensitive", True),
                normalized=args.get("normalized", False),
                max_results=args.get("max_results"),
            ),
        )


class MemoryAddAdHocNoteTool(_MemoryTool):
    name = "memory_add_ad_hoc_note"
    display_label = "Add memory note"
    activity_kind = "fileChange"
    mutates_external_state = True
    description = (
        "Create one append-only ad-hoc memory note after the user explicitly asks "
        "MiniCode to remember, forget, or update something."
    )

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "additionalProperties": False,
                "required": ["filename", "note"],
                "properties": {
                    "filename": {
                        "type": "string",
                        "minLength": 24,
                        "maxLength": 128,
                        "pattern": r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-[a-z0-9][a-z0-9-]{0,79}\.md$",
                        "description": (
                            "Name of the note file to create, in YYYY-MM-DDTHH-MM-SS-<slug>.md "
                            "format. The slug must use only lowercase ASCII letters, digits, and hyphens."
                        ),
                    },
                    "note": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Verbatim Markdown note to append to the ad-hoc memory notes.",
                    },
                },
            },
        )

    async def execute(self, args: dict[str, Any], context: ToolExecutionContext | None = None) -> ToolResult:
        filename = args.get("filename")
        note = args.get("note")
        if not isinstance(filename, str) or not isinstance(note, str):
            return self._error_result("filename and note are required")
        return await self._execute_backend(
            context,
            lambda backend: backend.add_ad_hoc_note(filename=filename, note=note),
            lock=True,
        )
