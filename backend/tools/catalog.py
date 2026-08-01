from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

from backend.tools.contracts import ToolSpec
from backend.agent.tool_common import WEB_SEARCH_TOOL_NAMES, WEB_FETCH_TOOL_NAMES

if TYPE_CHECKING:
    from backend.tools.registry import ToolRegistry


BRIDGE_TOOL_NAMES = {"tool_search", "tool_describe", "tool_call"}
COORDINATION_TOOL_NAMES = frozenset(
    {
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
        "read_artifact",
        "present_file",
        "write_file",
        "edit_file",
        "grep_files",
        "glob_files",
        "run_command",
        "monitor",
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
    if tool_name in COORDINATION_TOOL_NAMES:
        return ToolSpec(
            name=tool_name,
            capability=f"agent.coordination.{tool_name}",
            toolset="agent",
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
            toolset="artifact",
            # read_file and large tool results explicitly hand the model an
            # artifact_id. Recovery must not require a separate
            # tool_search -> tool_describe -> tool_call protocol round-trip.
            exposure="core",
            required_args=("artifact_id",),
        )
    return None


def _registered_tool_spec(tool_registry: ToolRegistry, tool_name: str) -> ToolSpec | None:
    tool = tool_registry.get_tool(tool_name)
    if tool is None:
        return None
    # Built-in names have authoritative static contracts. A lightweight test
    # double or plugin implementation must not replace their toolset/exposure
    # policy merely because it only supplies JSON Schema.
    if _static_core_spec(tool_name) is not None:
        return None
    get_spec = getattr(tool, "get_spec", None)
    spec = None
    if callable(get_spec):
        try:
            spec = get_spec()
        except Exception:
            spec = None
    if isinstance(spec, ToolSpec):
        if tool_name.startswith("mcp__") and spec.exposure == "core":
            # MCP schemas stay lazy so large connector catalogs do not flood
            # every provider turn, but their names remain discoverable through
            # the explicit deferred-tool directory. Hidden made installed MCP
            # capabilities impossible for the model to reach at all.
            return replace(spec, exposure="deferred", toolset="mcp")
        return spec

    # Third-party and local tools commonly provide only a JSON Schema. Preserve
    # its required arguments in the runtime contract so repair, validation, and
    # model history all agree on what a valid call looks like.
    try:
        schema = tool.get_schema()
        parameters = getattr(schema, "parameters", None)
        if not isinstance(parameters, dict) and isinstance(schema, dict):
            parameters = schema.get("parameters")
    except Exception:
        parameters = None
    if not isinstance(parameters, dict):
        return None
    raw_required = parameters.get("required")
    required = tuple(
        str(arg).strip()
        for arg in raw_required
        if isinstance(arg, str) and str(arg).strip()
    ) if isinstance(raw_required, list) else ()
    return ToolSpec(
        name=tool_name,
        toolset="mcp" if tool_name.startswith("mcp__") else "default",
        exposure=_exposure_for_name(tool_name),  # type: ignore[arg-type]
        required_args=required,
    )


def _exposure_for_name(tool_name: str) -> str:
    if tool_name.startswith("mcp__"):
        return "deferred"
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
        )
    if tool_name in WEB_FETCH_TOOL_NAMES:
        return ToolSpec(
            name=tool_name,
            capability="web.fetch",
            toolset="web",
            exposure="core",
            required_args=("url",),
        )

    return ToolSpec(
        name=tool_name,
        capability="",
        toolset="mcp" if tool_name.startswith("mcp__") else "default",
        exposure=_exposure_for_name(tool_name),  # type: ignore[arg-type]
        required_args=(),
    )


def schema_name(schema: dict[str, Any]) -> str:
    return str((schema.get("function") or {}).get("name") or "")
