from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

from backend.tools.contracts import ToolSpec

if TYPE_CHECKING:
    from backend.tools.registry import ToolRegistry


BRIDGE_TOOL_NAMES = {"tool_search"}
def _registered_tool_spec(tool_registry: ToolRegistry, tool_name: str) -> ToolSpec:
    tool = tool_registry.get_tool(tool_name)
    if tool is None:
        raise KeyError(f"Tool is not registered: {tool_name}")
    get_spec = getattr(tool, "get_spec", None)
    spec = get_spec() if callable(get_spec) else None
    if isinstance(spec, ToolSpec):
        if spec.name != tool_name:
            raise ValueError(
                f"Tool spec name mismatch: registry={tool_name}, spec={spec.name}"
            )
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
    schema = tool.get_schema()
    parameters = getattr(schema, "parameters", None)
    if not isinstance(parameters, dict) and isinstance(schema, dict):
        parameters = schema.get("parameters")
    if not isinstance(parameters, dict):
        raise ValueError(f"Tool {tool_name} does not expose an object schema")
    raw_required = parameters.get("required")
    required = tuple(
        str(arg).strip()
        for arg in raw_required
        if isinstance(arg, str) and str(arg).strip()
    ) if isinstance(raw_required, list) else ()
    # MCP capabilities are installed dynamically and stay behind the shared
    # deferred directory unless the tool explicitly opts into always-load.
    # Keep this rule at the canonical registry boundary so a plain BaseTool
    # implementation cannot accidentally flood the provider schema.
    is_mcp = tool_name.startswith("mcp__")
    return ToolSpec(
        name=tool_name,
        toolset="mcp" if is_mcp else "default",
        exposure=(
            "core"
            if getattr(tool, "always_load", False)
            else "deferred"
            if is_mcp or getattr(tool, "should_defer", False)
            else "core"
        ),
        required_args=required,
    )


def tool_spec_for(tool_name: str, tool_registry: ToolRegistry) -> ToolSpec:
    """Return runtime metadata for one registered MiniCode tool."""
    spec = _registered_tool_spec(tool_registry, tool_name)
    tool = tool_registry.get_tool(tool_name)
    if tool is None:
        raise KeyError(f"Tool is not registered: {tool_name}")
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


def schema_name(schema: dict[str, Any]) -> str:
    return str((schema.get("function") or {}).get("name") or "")


def canonicalize_tool_schemas(
    schemas: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None,
    *,
    tool_registry: "ToolRegistry | None" = None,
) -> list[dict[str, Any]]:
    """Return the stable tool wire order used by mature agent runtimes.

    Core/built-in tools form one contiguous prefix; MCP/deferred connector
    tools follow it in lexical order.  Object keys are recursively ordered,
    while schema lists retain their semantic order.  Duplicate names keep the
    first registration, matching the registry's precedence instead of making
    the provider payload depend on incidental insertion order.
    """

    def canonical(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                str(key): canonical(item)
                for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            }
        if isinstance(value, list):
            return [canonical(item) for item in value]
        if isinstance(value, tuple):
            return [canonical(item) for item in value]
        return value

    selected: dict[str, dict[str, Any]] = {}
    unnamed: list[dict[str, Any]] = []
    for raw in schemas or ():
        if not isinstance(raw, dict):
            continue
        item = canonical(raw)
        name = schema_name(item).strip()
        if not name:
            unnamed.append(item)
            continue
        if name not in selected:
            selected[name] = item

    def is_mcp(name: str) -> bool:
        if name.startswith("mcp__"):
            return True
        if tool_registry is not None:
            return tool_spec_for(name, tool_registry).toolset == "mcp"
        return False

    ordered = sorted(
        selected.items(),
        key=lambda pair: (1 if is_mcp(pair[0]) else 0, pair[0]),
    )
    # Schemas without a name are invalid for function calling but retaining
    # them at the end preserves the provider's existing validation behavior.
    return [schema for _name, schema in ordered] + unnamed
