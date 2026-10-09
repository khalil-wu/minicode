"""Permission-aware derivation of turn-local tool schemas."""

from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass, replace
from typing import Any

from backend.agent.prompting import build_tool_runtime_guidance
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext
from backend.tools.registry import ToolRegistry
from backend.tools.catalog import canonicalize_tool_schemas
from backend.tools.tool_search import build_deferred_tools_prompt_block
from backend.tools.toolsets import ToolsetPolicy
from backend.llm.provider_contracts import normalize_tool_mode


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


def requested_tool_mode(*, default_code_mode_only: bool, model_execution: Any | None, llm: Any) -> str:
    configured_mode = getattr(llm, "configured_tool_mode", None)
    adapter_mode = configured_mode() if callable(configured_mode) else ""
    model_info = getattr(model_execution, "model_info", None)
    if model_info is not None:
        declared = str(
            getattr(model_info, "tool_mode", "")
            or getattr(model_execution.config.llm, "tool_mode", "")
            or adapter_mode
            or ""
        )
    elif model_execution is not None:
        declared = str(
            getattr(model_execution.config.llm, "tool_mode", "")
            or adapter_mode
            or ""
        )
    else:
        declared = str(adapter_mode or "")
    return normalize_tool_mode(declared) or ("code_mode_only" if default_code_mode_only else "code_mode")


def effective_toolset_policy(
    *,
    base_policy: ToolsetPolicy | None,
    tool_registry: ToolRegistry,
    disabled_tools: set[str],
    requires_explicit_workspace: bool,
    workspace_root: Any | None,
    permission_mode: str,
    tool_mode: str = "code_mode",
    hosted_web_search: bool = False,
) -> ToolsetPolicy:
    """Build the one policy used by schema, discovery, and execution."""

    denied = set(disabled_tools)
    from backend.tools.web_tools import WebSearchTool

    search_tool = tool_registry.get_tool("web_search")
    if isinstance(search_tool, WebSearchTool) and not search_tool.source_available(hosted_search=hosted_web_search):
        denied.add("web_search")
    if (
        requires_explicit_workspace
        and workspace_root is None
        and permission_mode != "bypass"
    ):
        denied.update(workspace_bound_tool_names(tool_registry))
    policy = (base_policy or ToolsetPolicy.default()).with_disabled_tools(denied)
    mode = tool_mode if tool_registry.get_tool("tool_exec") is not None else "direct"
    return replace(
        policy,
        code_mode_only=mode == "code_mode_only",
        code_mode_enabled=mode != "direct",
    )


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
    derivation_key: tuple[Any, ...] = ()


def derive_turn_tool_schema_state(
    *,
    base_tool_schemas: list[dict[str, Any]],
    mcp_instructions: dict[str, str],
    tool_registry: ToolRegistry | None = None,
    permission_checker: PermissionChecker | None = None,
    permission_context: PermissionContext | None = None,
    toolset_policy: Any | None = None,
    mcp_registry_version: int = 0,
    previous: TurnToolSchemaDerivation | None = None,
) -> TurnToolSchemaDerivation:
    permission_key = permission_context_cache_key(permission_context)
    derivation_key = (
        tool_registry.schema_source if tool_registry is not None else None,
        tool_registry.version if tool_registry is not None else None,
        mcp_registry_version,
        toolset_policy.cache_key() if toolset_policy is not None else "",
        id(permission_checker), tuple(sorted(mcp_instructions.items())),
    )
    if (previous is not None and previous.permission_key == permission_key
            and previous.derivation_key == derivation_key
            and previous.tool_schemas == base_tool_schemas):
        return previous
    canonical_base_schemas = canonicalize_tool_schemas(
        base_tool_schemas,
        tool_registry=tool_registry,
    )
    schema_key = tuple(
        json.dumps(schema, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        for schema in canonical_base_schemas
    )
    names = sorted(tool_schema_names(canonical_base_schemas))
    deferred = ""
    if "tool_search" in names and tool_registry is not None:
        deferred = build_deferred_tools_prompt_block(
            tool_registry,
            toolset_policy=toolset_policy,
            permission_checker=permission_checker,
            permission_context=permission_context,
        )
    reachable_mcp_tools = set()
    if tool_registry is not None:
        reachable_mcp_tools = {
            view.name for view in tool_registry.build_schema_views(
                toolset_policy=toolset_policy,
                permission_checker=permission_checker,
                permission_context=permission_context,
                materialize_schema=False,
            )
            if view.name.startswith("mcp__") and (view.direct or view.code_mode_available
                or ("tool_search" in names and view.exposure in {"deferred", "deferred_model_only"}))
        }
    return TurnToolSchemaDerivation(
        permission_key=permission_key,
        schema_key=schema_key,
        tool_schemas=canonical_base_schemas,
        tool_names=names,
        runtime_guidance=build_tool_runtime_guidance(canonical_base_schemas, mcp_instructions,
            reachable_mcp_tools=reachable_mcp_tools),
        deferred_tools_prompt_block=deferred,
        derivation_key=derivation_key,
    )
