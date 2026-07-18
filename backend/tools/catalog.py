from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

from backend.tools.contracts import ToolSpec
from backend.agent.tool_common import WEB_SEARCH_TOOL_NAMES, WEB_FETCH_TOOL_NAMES

if TYPE_CHECKING:
    from backend.tools.registry import ToolRegistry


BRIDGE_TOOL_NAMES = {"tool_search", "tool_describe", "tool_call"}
COORDINATOR_ONLY_TOOL_NAMES = frozenset(
    {
        "workflow",
        "message_list",
        "task_create",
        "task_list",
        "task_get",
        "task_update",
        "task_output",
        "team_create",
        "team_list",
        "team_delete",
    }
)
CORE_TOOL_NAMES = frozenset(
    {
        "read_file",
        "write_file",
        "edit_file",
        "grep_files",
        "glob_files",
        "run_command",
        "web_search",
        "web_fetch",
        "todo_write",
        "tool_search",
        "tool_describe",
        "tool_call",
        "sleep",
    }
)


def _static_core_spec(tool_name: str) -> ToolSpec | None:
    """Known core fallback specs for test doubles and lightweight registries."""
    if tool_name in COORDINATOR_ONLY_TOOL_NAMES:
        return ToolSpec(
            name=tool_name,
            capability=f"agent.coordinator.{tool_name}",
            toolset="coordinator",
            exposure="deferred",
        )
    if tool_name == "task_status":
        return ToolSpec(
            name=tool_name,
            capability="agent.status",
            toolset="agent",
            exposure="core",
        )
    if tool_name == "task_stop":
        return ToolSpec(
            name=tool_name,
            capability="agent.stop",
            toolset="agent",
            exposure="core",
        )
    if tool_name == "send_message":
        return ToolSpec(
            name=tool_name,
            capability="agent.message",
            toolset="agent",
            exposure="core",
        )
    if tool_name == "read_artifact":
        return ToolSpec(
            name=tool_name,
            capability="artifact.read",
            exposure="core",
            required_args=("artifact_id",),
            arg_roles={"artifact_id": "latest_artifact"},
            repair_policy={"artifact_id": "resource_resolver"},
            accepted_resource_types=("artifact",),
            empty_args_policy="repair_or_block",
        )
    return None


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
    """Return runtime metadata for a tool, overlaying cc-style visibility hints."""
    spec = _tool_spec_for_impl(tool_name, tool_registry)
    tool = tool_registry.get_tool(tool_name)
    if tool is not None:
        updated = spec
        if (
            getattr(tool, "should_defer", False)
            and updated.exposure == "core"
            and not updated.always_load
            and not getattr(tool, "always_load", False)
        ):
            updated = replace(
                updated,
                exposure="deferred",
                toolset=updated.toolset if updated.toolset != "core" else "default",
            )
        if getattr(tool, "always_load", False) and not updated.always_load:
            updated = replace(updated, always_load=True)
        return updated
    return spec


def _tool_spec_for_impl(tool_name: str, tool_registry: ToolRegistry) -> ToolSpec:
    """Return runtime metadata for a tool without materializing JSON schema."""
    registered = _registered_tool_spec(tool_registry, tool_name)
    if registered is not None:
        return registered
    static = _static_core_spec(tool_name)
    if static is not None:
        return static

    if tool_name in WEB_SEARCH_TOOL_NAMES:
        return ToolSpec(
            name=tool_name,
            capability="web.search",
            toolset="web",
            exposure="core",
            required_args=("query",),
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
            required_args=("url",),
            arg_roles={"url": "latest_url"},
            arg_sources={"url": ("previous_search_result",)},
            repair_policy={"url": "resource_resolver"},
            accepted_resource_types=("web_url",),
            empty_args_policy="repair_or_block",
            blocked_guidance="Missing URL. Fetch a known URL from previous search results.",
        )

    return ToolSpec(
        name=tool_name,
        capability="",
        toolset="mcp" if tool_name.startswith("mcp__") else "default",
        exposure=_exposure_for_name(tool_name),  # type: ignore[arg-type]
        required_args=(),
        arg_roles={},
        repair_policy={},
        empty_args_policy="block",
    )


def schema_name(schema: dict[str, Any]) -> str:
    return str((schema.get("function") or {}).get("name") or "")
