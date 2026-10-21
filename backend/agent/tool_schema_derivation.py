"""Permission-aware derivation of turn-local tool schemas."""

from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass, fields, replace
from typing import Any

from backend.agent.prompting import build_tool_runtime_guidance
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext
from backend.tools.registry import ToolRegistry
from backend.tools.catalog import canonicalize_tool_schemas
from backend.tools.toolsets import ToolsetPolicy
from backend.tools.toolsets import SOURCE_MESSAGE_BOARD_TOOLS, SOURCE_V2_COORDINATION_TOOLS
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
    "spawn_agent",
)


def permission_context_cache_key(context: PermissionContext | None) -> tuple[Any, ...]:
    if context is None:
        return ()
    # Schema visibility depends on approval policy, filesystem scope and sandbox
    # settings as well as mode. Use the complete turn permission contract.
    return (json.dumps(
        {field.name: getattr(context, field.name) for field in fields(context)},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
    ),)


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
    hosted_cached_search: bool = False,
    supports_search_tool: bool = False,
    model_execution: Any | None = None,
    multi_agent_version: str = "",
    config_snapshot: Any | None = None,
    is_subagent: bool = False,
    realtime_service: Any | None = None,
) -> ToolsetPolicy:
    """Build the one policy used by schema, discovery, and execution."""

    denied = set(disabled_tools)
    coordination_options = None
    if model_execution is not None:
        from backend.agents.source_roles import source_agent_options
        from backend.llm.model_request_policy import source_model_request_capabilities
        from backend.tools.collaboration_tools import SpawnAgentTool, ListAgentsTool, WaitAgentTool, InterruptAgentTool
        from backend.tools.agent_user_tools import RequestUserInputAsyncTool, SendMessageToUserAsyncTool
        from backend.tools.sleep_tool import CurrentTimeTool
        from backend.tools.swarm_tools import FollowupTaskTool, SourceSendMessageTool
        config = model_execution.config
        coordination = source_agent_options(config, model=model_execution.model, definition=model_execution.model_info, version=multi_agent_version)
        v2_names = {"spawn_agent", "list_agents", "wait_agent", "interrupt_agent"}
        legacy_names = {"task", "task_status", "task_stop", "send_message", "followup_task", "message_list",
            "task_create", "task_list", "task_get", "task_update", "task_output", "team_create", "team_list", "team_delete"}
        if not coordination["enabled"]:
            denied.update(v2_names | legacy_names)
            denied.update(SOURCE_MESSAGE_BOARD_TOOLS)
        elif coordination["version"] == "v2":
            coordination_options = coordination
            if not isinstance(tool_registry.get_tool("send_message"), SourceSendMessageTool):
                tool_registry.register(SourceSendMessageTool(), replace=True)
            if tool_registry.get_tool("followup_task") is None:
                tool_registry.register(FollowupTaskTool())
            task = tool_registry.get_tool("task")
            if task is not None:
                for tool in (SpawnAgentTool(task), ListAgentsTool(), WaitAgentTool(config), InterruptAgentTool()):
                    if tool_registry.get_tool(tool.name) is None:
                        tool_registry.register(tool)
            denied.update(legacy_names - {"send_message", "followup_task"})
            if is_subagent and source_model_request_capabilities(model_execution.model,
                    model_execution.model_info).get("multi_agent_version") != "v2":
                denied.update(SOURCE_V2_COORDINATION_TOOLS)
            if not coordination["wait_agent_enabled"]:
                denied.add("wait_agent")
            if coordination["disable_direct_message"]:
                denied.update({"send_message", "followup_task"})
            if coordination["message_board_enabled"]:
                from backend.tools.agent_message_board_tools import SUPPORTED_BOARD_BACKENDS, message_board_tools

                if coordination["message_board_backend"] in SUPPORTED_BOARD_BACKENDS:
                    for tool in message_board_tools():
                        if tool_registry.get_tool(tool.name) is None:
                            tool_registry.register(tool)
                else:
                    denied.update(SOURCE_MESSAGE_BOARD_TOOLS)
            else:
                denied.update(SOURCE_MESSAGE_BOARD_TOOLS)
        else:
            denied.update(v2_names)
            denied.update(SOURCE_MESSAGE_BOARD_TOOLS)
        supported = source_model_request_capabilities(model_execution.model, model_execution.model_info).get("experimental_supported_tools", [])
        if "send_user_message_async" in supported or "request_user_input_async" in supported:
            for tool in (RequestUserInputAsyncTool(), SendMessageToUserAsyncTool()):
                if tool_registry.get_tool(tool.name) is None:
                    tool_registry.register(tool)
        else:
            denied.update({"request_user_input_async", "send_message_to_user_async"})
        if "clock" in supported and tool_registry.get_tool("curr_time") is None:
            tool_registry.register(CurrentTimeTool())
        elif "clock" not in supported:
            denied.add("curr_time")
    from backend.tools.web_tools import WebSearchTool

    search_tool = tool_registry.get_tool("web_search")
    if isinstance(search_tool, WebSearchTool):
        from backend.llm.model_request_policy import resolve_web_search_mode
        search_config = model_execution.config if model_execution is not None else config_snapshot
        search_mode = resolve_web_search_mode(search_config, permission_mode=permission_mode) if search_config is not None else "live"
        if search_mode == "disabled" or (search_mode == "cached" and not hosted_cached_search) or not search_tool.source_available(hosted_search=hosted_web_search):
            denied.add("web_search")
    if (
        requires_explicit_workspace
        and workspace_root is None
        and permission_mode != "bypass"
    ):
        denied.update(workspace_bound_tool_names(tool_registry))
    policy = replace(
        (base_policy or ToolsetPolicy.default()).with_disabled_tools(denied),
        realtime_service=realtime_service if not is_subagent else None,
    )
    if supports_search_tool and not any((spec := tool_registry.get_tool_spec(tool.name)).exposure in {"deferred", "deferred_model_only"}
            and policy.is_available(spec) for tool in tool_registry.get_tools()):
        policy = policy.with_disabled_tools({"tool_search"})
    mode = tool_mode if tool_registry.get_tool("tool_exec") is not None else "direct"
    return replace(
        policy,
        code_mode_only=mode == "code_mode_only",
        code_mode_enabled=mode != "direct",
        supports_search_tool=supports_search_tool,
        coordination_options=coordination_options,
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
        id(permission_checker),
        json.dumps(permission_checker.policy_snapshot(), sort_keys=True) if permission_checker is not None else "",
        tuple(sorted(mcp_instructions.items())),
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
    views = tool_registry.build_schema_views(
        toolset_policy=toolset_policy,
        permission_checker=permission_checker,
        permission_context=permission_context,
        materialize_schema=False,
    ) if tool_registry is not None else []
    reachable_mcp_tools = set()
    reachable_mcp_servers = None
    if tool_registry is not None:
        reachable_mcp_tools = {
            view.name for view in views
            if view.name.startswith("mcp__") and (view.direct or view.code_mode_available
                or ("tool_search" in names and view.exposure in {"deferred", "deferred_model_only"}))
        }
        reachable_mcp_servers = {
            server for name in reachable_mcp_tools
            if (server := getattr(tool_registry.get_tool(name), "server_name", ""))
        } or None
    return TurnToolSchemaDerivation(
        permission_key=permission_key,
        schema_key=schema_key,
        tool_schemas=canonical_base_schemas,
        tool_names=names,
        runtime_guidance=build_tool_runtime_guidance(canonical_base_schemas, mcp_instructions,
            reachable_mcp_tools=reachable_mcp_tools, reachable_mcp_servers=reachable_mcp_servers),
        deferred_tools_prompt_block="",
        derivation_key=derivation_key,
    )
