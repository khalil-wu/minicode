"""Permission-aware derivation of turn-local tool schemas."""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from typing import Any

from backend.agent.prompting import build_tool_runtime_guidance
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext
from backend.tools.registry import ToolRegistry
from backend.tools.tool_search import build_deferred_tools_prompt_block


WORKSPACE_REQUIRED_TOOL_PATTERNS = (
    "read_file",
    "write_file",
    "edit_file",
    "list_files",
    "grep_files",
    "glob_files",
    "fuzzy_search",
    "go_to_definition",
    "find_references",
    "git_*",
    "run_command",
    "terminal_*",
    "worktree_*",
    "workspace_*",
    "preview.*",
    "todo_write",
    "task",
)


def permission_context_cache_key(context: PermissionContext | None) -> tuple[Any, ...]:
    if context is None:
        return ("", (), (), "")
    return (
        str(getattr(context, "mode", "") or ""),
        tuple(sorted(str(rule) for rule in getattr(context, "tool_deny_rules", []) or [])),
        tuple(sorted(
            (str(key), str(getattr(value, "value", value)))
            for key, value in (getattr(context, "session_overrides", {}) or {}).items()
        )),
        str(getattr(context, "source", "") or ""),
    )


def filter_disabled_tool_schemas(
    schemas: list[dict[str, Any]],
    disabled_tools: set[str],
) -> list[dict[str, Any]]:
    if not disabled_tools:
        return schemas
    return [
        schema
        for schema in schemas
        if str((schema.get("function") or {}).get("name") or "") not in disabled_tools
    ]


def workspace_bound_tool_names(schemas: list[dict[str, Any]]) -> set[str]:
    names: set[str] = set()
    for schema in schemas:
        name = str((schema.get("function") or {}).get("name") or "")
        if name and any(fnmatch.fnmatch(name, pattern) for pattern in WORKSPACE_REQUIRED_TOOL_PATTERNS):
            names.add(name)
    return names


def tool_schema_names(schemas: list[dict[str, Any]]) -> set[str]:
    return {
        name
        for schema in schemas
        if (name := str((schema.get("function") or {}).get("name") or ""))
    }


@dataclass(frozen=True)
class TurnToolSchemaDerivation:
    disabled_key: tuple[str, ...]
    permission_key: tuple[Any, ...]
    tool_schemas: list[dict[str, Any]]
    tool_names: list[str]
    runtime_guidance: str
    deferred_tools_prompt_block: str = ""


def derive_turn_tool_schema_state(
    *,
    base_tool_schemas: list[dict[str, Any]],
    disabled_tools: set[str],
    mcp_instructions: dict[str, str],
    tool_registry: ToolRegistry | None = None,
    permission_checker: PermissionChecker | None = None,
    permission_context: PermissionContext | None = None,
    toolset_policy: Any | None = None,
    previous: TurnToolSchemaDerivation | None = None,
) -> TurnToolSchemaDerivation:
    disabled_key = tuple(sorted(disabled_tools))
    permission_key = permission_context_cache_key(permission_context)
    if (
        previous is not None
        and previous.disabled_key == disabled_key
        and previous.permission_key == permission_key
    ):
        return previous
    schemas = filter_disabled_tool_schemas(base_tool_schemas, disabled_tools)
    names = sorted(tool_schema_names(schemas))
    deferred = ""
    if "tool_search" in names and tool_registry is not None:
        deferred = build_deferred_tools_prompt_block(
            tool_registry,
            toolset_policy=toolset_policy,
            permission_checker=permission_checker,
            permission_context=permission_context,
        )
    return TurnToolSchemaDerivation(
        disabled_key=disabled_key,
        permission_key=permission_key,
        tool_schemas=schemas,
        tool_names=names,
        runtime_guidance=build_tool_runtime_guidance(schemas, mcp_instructions),
        deferred_tools_prompt_block=deferred,
    )
