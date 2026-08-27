"""Permission-aware derivation of turn-local tool schemas."""

from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass
from typing import Any

from backend.agent.prompting import build_tool_runtime_guidance
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext
from backend.tools.registry import ToolRegistry
from backend.tools.catalog import canonicalize_tool_schemas
from backend.tools.tool_search import build_deferred_tools_prompt_block
from backend.tools.toolsets import ToolsetPolicy


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
    "lsp_*",
    "git_*",
    "run_command",
    "terminal_*",
    "read_terminal",
    "*worktree*",
    "workspace_*",
    "preview_*",
    "apply_patch",
    "notebook_edit",
    "enter_plan_mode",
    "exit_plan_mode",
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


def workspace_bound_tool_names(tool_registry: ToolRegistry) -> set[str]:
    """Return workspace capabilities from the complete registered surface."""

    names: set[str] = set()
    for name in tool_registry.list_tools():
        tool = tool_registry.get_tool(name)
        has_workspace_path = bool(
            tool is not None and getattr(tool, "workspace_path_fields", ())
        )
        if has_workspace_path or any(
            fnmatch.fnmatch(name, pattern)
            for pattern in WORKSPACE_REQUIRED_TOOL_PATTERNS
        ):
            names.add(name)
    return names


def effective_toolset_policy(
    *,
    base_policy: ToolsetPolicy | None,
    tool_registry: ToolRegistry,
    disabled_tools: set[str],
    requires_explicit_workspace: bool,
    workspace_root: Any | None,
    permission_mode: str,
) -> ToolsetPolicy:
    """Build the one policy used by schema, discovery, and execution."""

    denied = set(disabled_tools)
    if (
        requires_explicit_workspace
        and workspace_root is None
        and permission_mode != "bypass"
    ):
        denied.update(workspace_bound_tool_names(tool_registry))
    return (base_policy or ToolsetPolicy.default()).with_disabled_tools(denied)


def tool_schema_names(schemas: list[dict[str, Any]]) -> set[str]:
    return {
        name
        for schema in schemas
        if (name := str((schema.get("function") or {}).get("name") or ""))
    }


@dataclass(frozen=True)
class TurnToolSchemaDerivation:
    permission_key: tuple[Any, ...]
    schema_key: tuple[str, ...]
    tool_schemas: list[dict[str, Any]]
    tool_names: list[str]
    runtime_guidance: str
    deferred_tools_prompt_block: str = ""


def derive_turn_tool_schema_state(
    *,
    base_tool_schemas: list[dict[str, Any]],
    mcp_instructions: dict[str, str],
    tool_registry: ToolRegistry | None = None,
    permission_checker: PermissionChecker | None = None,
    permission_context: PermissionContext | None = None,
    toolset_policy: Any | None = None,
    previous: TurnToolSchemaDerivation | None = None,
) -> TurnToolSchemaDerivation:
    permission_key = permission_context_cache_key(permission_context)
    canonical_base_schemas = canonicalize_tool_schemas(
        base_tool_schemas,
        tool_registry=tool_registry,
    )
    schema_key = tuple(
        json.dumps(schema, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        for schema in canonical_base_schemas
    )
    if (
        previous is not None
        and previous.permission_key == permission_key
        and previous.schema_key == schema_key
    ):
        return previous
    names = sorted(tool_schema_names(canonical_base_schemas))
    deferred = ""
    if "tool_search" in names and tool_registry is not None:
        deferred = build_deferred_tools_prompt_block(
            tool_registry,
            toolset_policy=toolset_policy,
            permission_checker=permission_checker,
            permission_context=permission_context,
        )
    return TurnToolSchemaDerivation(
        permission_key=permission_key,
        schema_key=schema_key,
        tool_schemas=canonical_base_schemas,
        tool_names=names,
        runtime_guidance=build_tool_runtime_guidance(canonical_base_schemas, mcp_instructions),
        deferred_tools_prompt_block=deferred,
    )
