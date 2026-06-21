from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

from backend.tools.contracts import ToolSpec
from backend.agent.tool_common import WEB_SEARCH_TOOL_NAMES, WEB_FETCH_TOOL_NAMES

if TYPE_CHECKING:
    from backend.tools.registry import ToolRegistry


BRIDGE_TOOL_NAMES = {"tool_search", "tool_describe", "tool_call"}
CORE_TOOL_NAMES = frozenset(
    {
        "read_file",
        "write_file",
        "edit_file",
        "list_files",
        "grep_files",
        "glob_files",
        "run_command",
        "ask_user",
        "read_artifact",
        "web_search",
        "web_fetch",
        "todo_write",
        "tool_search",
        "tool_describe",
        "tool_call",
    }
)


def _schema_required_args(tool_registry: ToolRegistry, tool_name: str) -> tuple[str, ...]:
    tool = tool_registry.get_tool(tool_name)
    if tool is None:
        return ()
    try:
        schema = tool.get_schema()
    except Exception:
        return ()
    required = schema.parameters.get("required", []) if schema else []
    return tuple(str(field) for field in required if isinstance(field, str))


def _registered_tool_spec(tool_registry: ToolRegistry, tool_name: str) -> ToolSpec | None:
    tool = tool_registry.get_tool(tool_name)
    get_spec = getattr(tool, "get_spec", None)
    if not callable(get_spec):
        return None
    try:
        spec = get_spec()
    except Exception:
        return None
    if isinstance(spec, ToolSpec):
        if tool_name.startswith("mcp__") and spec.exposure == "core":
            return replace(spec, exposure="hidden", toolset="mcp")
        return spec
    return None


def _infer_arg_roles(required: tuple[str, ...]) -> dict[str, str]:
    roles: dict[str, str] = {}
    for arg in required:
        lower = arg.lower()
        if lower in {"file_path", "filepath", "path"} or lower.endswith("_path"):
            roles[arg] = "workspace_file"
        elif lower in {"url", "href", "link"}:
            roles[arg] = "latest_url"
        elif lower in {"query", "q", "search_query", "pattern"}:
            roles[arg] = "search_query"
        elif lower in {"artifact_id", "artifact_ref"}:
            roles[arg] = "latest_artifact"
        elif lower in {"content", "text", "body", "prompt", "description", "old_string", "new_string"}:
            roles[arg] = "generated_content"
    return roles


def _exposure_for_name(tool_name: str) -> str:
    if tool_name.startswith("mcp__"):
        return "hidden"
    if tool_name in CORE_TOOL_NAMES:
        return "core"
    return "deferred"


def tool_spec_for(tool_name: str, tool_registry: ToolRegistry) -> ToolSpec:
    """Return runtime metadata for a tool, overlaying the tool's always_load hint."""
    spec = _tool_spec_for_impl(tool_name, tool_registry)
    tool = tool_registry.get_tool(tool_name)
    if tool is not None and getattr(tool, "always_load", False) and not spec.always_load:
        from dataclasses import replace

        return replace(spec, always_load=True)
    return spec


def _tool_spec_for_impl(tool_name: str, tool_registry: ToolRegistry) -> ToolSpec:
    """Return runtime metadata for a tool, falling back to schema-derived roles."""
    registered = _registered_tool_spec(tool_registry, tool_name)
    if registered is not None:
        return registered

    required = _schema_required_args(tool_registry, tool_name)
    if tool_name in WEB_SEARCH_TOOL_NAMES:
        return ToolSpec(
            name=tool_name,
            capability="web.search",
            toolset="web",
            exposure="core",
            required_args=required or ("query",),
            arg_roles={"query": "search_query"},
            arg_sources={"query": ("user_message", "search_plan")},
            repair_policy={"query": "resource_resolver"},
            accepted_resource_types=("search_need",),
            empty_args_policy="repair_or_block",
            blocked_guidance="Missing query. Use a concrete query derived from the user request.",
        )
    if tool_name in WEB_FETCH_TOOL_NAMES:
        return ToolSpec(
            name=tool_name,
            capability="web.fetch",
            toolset="web",
            exposure="core",
            required_args=required or ("url",),
            arg_roles={"url": "latest_url"},
            arg_sources={"url": ("previous_search_result",)},
            repair_policy={"url": "resource_resolver"},
            accepted_resource_types=("web_url",),
            empty_args_policy="repair_or_block",
            blocked_guidance="Missing URL. Fetch a known URL from previous search results.",
        )

    inferred_roles = _infer_arg_roles(required)
    return ToolSpec(
        name=tool_name,
        capability="",
        toolset="mcp" if tool_name.startswith("mcp__") else "default",
        exposure=_exposure_for_name(tool_name),  # type: ignore[arg-type]
        required_args=required,
        arg_roles=inferred_roles,
        repair_policy={
            arg: ("needs_model_generation" if role == "generated_content" else "resource_resolver")
            for arg, role in inferred_roles.items()
        },
        empty_args_policy="repair_or_block" if inferred_roles else "block",
    )


def schema_name(schema: dict[str, Any]) -> str:
    return str((schema.get("function") or {}).get("name") or "")
