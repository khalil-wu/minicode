"""Capability registry for tools plus command/skill metadata."""

from __future__ import annotations

import json
import logging
import inspect
from collections import OrderedDict
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from backend.permissions.checker import PermissionChecker
    from backend.permissions.context import PermissionContext

from backend.permissions.context import ToolExecutionContext
from backend.tools.base import BaseTool, ToolResult, PermissionLevel

logger = logging.getLogger(__name__)

RESULT_CACHE_MAXSIZE = 128
_CACHEABLE_TOOL_NAMES = {"read_file", "list_files", "grep_files", "glob_files", "web_fetch"}
_MUTATING_TOOL_NAMES = {"write_file", "edit_file"}


class CapabilityRegistry:
    """In-memory registry for tools, commands, and skills."""

    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}
        self._commands: dict[str, Any] = {}
        self._skills: dict[str, dict[str, Any]] = {}
        self._schema_cache: dict[str, list[dict[str, Any]]] = {}
        self._result_cache: OrderedDict[str, ToolResult] = OrderedDict()
        self._result_cache_paths: dict[str, set[str]] = {}
        self._version = 0

    def register(self, tool: BaseTool) -> None:
        if tool.name in self._tools:
            logger.warning(
                "Tool name conflict detected for '%s'; overriding previous registration",
                tool.name,
            )
        self._tools[tool.name] = tool
        self._touch()

    def unregister(self, name: str) -> bool:
        removed = self._tools.pop(name, None) is not None
        if removed:
            self._touch()
            self._clear_result_cache()
        return removed

    def register_command(self, name: str, handler: Any) -> None:
        if name in self._commands:
            logger.warning("Command name conflict detected for '%s'; overriding previous registration", name)
        self._commands[name] = handler
        self._touch()

    def unregister_command(self, name: str) -> bool:
        removed = self._commands.pop(name, None) is not None
        if removed:
            self._touch()
        return removed

    def get_command(self, name: str) -> Any | None:
        return self._commands.get(name)

    def list_commands(self) -> list[str]:
        return list(self._commands.keys())

    def get_commands(self) -> dict[str, Any]:
        return dict(self._commands)

    def register_skill(self, name: str, definition: dict[str, Any]) -> None:
        if name in self._skills:
            logger.warning("Skill name conflict detected for '%s'; overriding previous registration", name)
        self._skills[name] = dict(definition)
        self._touch()

    def unregister_skill(self, name: str) -> bool:
        removed = self._skills.pop(name, None) is not None
        if removed:
            self._touch()
        return removed

    def get_skill(self, name: str) -> dict[str, Any] | None:
        skill = self._skills.get(name)
        return dict(skill) if skill is not None else None

    def list_skills(self) -> list[str]:
        return list(self._skills.keys())

    def get_skills(self) -> dict[str, dict[str, Any]]:
        return {name: dict(definition) for name, definition in self._skills.items()}

    def build_snapshot(self, budget: int = 6000) -> dict[str, Any]:
        """Return a stable capability snapshot for UI/runtime consumers."""
        return {
            "version": self._version,
            "tools": deepcopy(self.get_schemas(budget)),
            "commands": [
                self._build_named_metadata(name, metadata)
                for name, metadata in sorted(self._commands.items())
            ],
            "skills": [
                self._build_named_metadata(name, definition)
                for name, definition in sorted(self._skills.items())
            ],
        }

    def has_tool(self, name: str) -> bool:
        return name in self._tools

    def get_tool(self, name: str) -> BaseTool | None:
        return self._tools.get(name)

    def get_tools(self) -> list[BaseTool]:
        return list(self._tools.values())

    # 核心工具列表 — 始终获得完整 schema（参考 Claude Code 核心工具优先级）
    _CORE_TOOLS = frozenset({
        "read_file", "write_file", "edit_file", "list_files",
        "run_command", "ask_user", "grep_files", "glob_files",
        "todo_write", "task", "tool_search",
        "go_to_definition", "find_references",
        "web_search",
    })

    def get_schemas(
        self,
        budget: int = 6000,
        permission_checker: 'PermissionChecker | None' = None,
        permission_context: 'PermissionContext | None' = None,
    ) -> list[dict[str, Any]]:
        # 生成缓存键，包含权限拦截配置的模式
        cache_key = f"{budget}_{self._version}"
        if permission_checker and permission_context:
            overrides_hash = hash(frozenset(permission_context.session_overrides.items())) if permission_context.session_overrides else 0
            cache_key = (
                f"{budget}_{self._version}_{permission_context.mode}"
                f"_{hash(frozenset(permission_context.tool_deny_rules))}"
                f"_{overrides_hash}"
            )

        cached = self._schema_cache.get(cache_key)
        if cached is not None:
            return cached

        # 将工具分为核心工具和普通工具，核心工具优先获得完整 schema
        core_tools: list[BaseTool] = []
        other_tools: list[BaseTool] = []
        for tool in self._tools.values():
            if permission_checker and permission_context:
                level = permission_checker.check(tool.name, context=permission_context)
                if level == PermissionLevel.ALWAYS_DENY:
                    continue
            if tool.name in self._CORE_TOOLS:
                core_tools.append(tool)
            else:
                other_tools.append(tool)

        schemas: list[dict[str, Any]] = []
        estimated_tokens = 0

        # 第一轮：核心工具完整 schema
        for tool in core_tools:
            schema = tool.get_schema()
            full_schema = schema.to_openai_tool()
            schema_tokens = len(str(full_schema)) // 4
            schemas.append(full_schema)
            estimated_tokens += schema_tokens

        # 第二轮：普通工具在预算内尽量给完整 schema，超出则精简
        for tool in other_tools:
            schema = tool.get_schema()
            full_schema = schema.to_openai_tool()
            schema_tokens = len(str(full_schema)) // 4

            if estimated_tokens + schema_tokens <= budget:
                schemas.append(full_schema)
                estimated_tokens += schema_tokens
                continue

            schemas.append(
                {
                    "type": "function",
                    "function": {
                        "name": schema.name,
                        "description": schema.description.split(".")[0],
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            )
            estimated_tokens += 20

        self._schema_cache[cache_key] = schemas
        return schemas

    async def execute(
        self,
        name: str,
        args: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(
                content=f"Tool '{name}' does not exist. Available: {', '.join(self._tools.keys())}",
                is_error=True,
            )

        cache_key = self._build_result_cache_key(name, args, context) if name in _CACHEABLE_TOOL_NAMES else None
        if cache_key is not None:
            cached = self._result_cache.get(cache_key)
            if cached is not None:
                self._result_cache.move_to_end(cache_key)
                return replace(cached)

        try:
            if self._tool_accepts_context(tool):
                result = await tool.execute(args, context=context)
            else:
                result = await tool.execute(args)
        except Exception as exc:
            return ToolResult(
                content=(
                    f"Tool '{name}' execution failed: {exc}\n"
                    "Check the arguments or try a different approach."
                ),
                is_error=True,
            )

        if cache_key is not None and not result.is_error:
            self._store_result_cache(cache_key, args, result)
        elif name in _MUTATING_TOOL_NAMES and not result.is_error:
            self._invalidate_result_cache(args)

        return result

    def list_tools(self) -> list[str]:
        return list(self._tools.keys())

    @property
    def count(self) -> int:
        return len(self._tools)

    @property
    def version(self) -> int:
        return self._version

    def _build_result_cache_key(
        self,
        name: str,
        args: dict[str, Any],
        context: ToolExecutionContext | None = None,
    ) -> str:
        workspace_root = ""
        if context and context.workspace_root:
            workspace_root = Path(context.workspace_root).resolve().as_posix()
        payload = {"workspace_root": workspace_root, "args": args}
        return f"{name}:{json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)}"

    def _store_result_cache(self, cache_key: str, args: dict[str, Any], result: ToolResult) -> None:
        self._result_cache[cache_key] = replace(result)
        self._result_cache.move_to_end(cache_key)
        self._result_cache_paths[cache_key] = self._extract_related_paths(args)
        while len(self._result_cache) > RESULT_CACHE_MAXSIZE:
            oldest_key, _ = self._result_cache.popitem(last=False)
            self._result_cache_paths.pop(oldest_key, None)

    def _invalidate_result_cache(self, args: dict[str, Any]) -> None:
        related_paths = self._extract_related_paths(args)
        if not related_paths:
            self._clear_result_cache()
            return

        stale_keys = [
            cache_key
            for cache_key, cached_paths in self._result_cache_paths.items()
            if related_paths & cached_paths
        ]
        for cache_key in stale_keys:
            self._result_cache.pop(cache_key, None)
            self._result_cache_paths.pop(cache_key, None)

    def _clear_result_cache(self) -> None:
        self._result_cache.clear()
        self._result_cache_paths.clear()

    def _extract_related_paths(self, args: dict[str, Any]) -> set[str]:
        paths: set[str] = set()
        for key, value in args.items():
            if not isinstance(value, str) or not value.strip():
                continue
            lowered = key.lower()
            if lowered in {"file_path", "path", "directory", "cwd", "root"} or lowered.endswith("_path"):
                paths.add(self._normalize_path(value))
        return paths

    def _normalize_path(self, raw_path: str) -> str:
        if "://" in raw_path:
            return raw_path.strip()
        return Path(raw_path).as_posix()

    def _tool_accepts_context(self, tool: BaseTool) -> bool:
        try:
            return "context" in inspect.signature(tool.execute).parameters
        except (TypeError, ValueError):
            return False

    def _build_named_metadata(self, name: str, metadata: Any) -> dict[str, Any]:
        if isinstance(metadata, dict):
            payload = dict(metadata)
        elif metadata is None:
            payload = {}
        else:
            payload = {"handler": getattr(metadata, "__name__", metadata.__class__.__name__)}
        payload["name"] = name
        return payload

    def _touch(self) -> None:
        self._version += 1
        self._schema_cache.clear()


ToolRegistry = CapabilityRegistry
