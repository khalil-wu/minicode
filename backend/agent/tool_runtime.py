from __future__ import annotations

from backend.tools.registry import ToolRegistry

def resolve_tool_timeout(
    name: str,
    tool_registry: ToolRegistry,
    args: dict[str, object] | None = None,
) -> float | None:
    """Resolve only the timeout owned by the tool declaration.

    An undeclared timeout is intentionally unbounded here.  The turn's
    absolute deadline, when configured, remains the sole runtime boundary.
    """
    tool = tool_registry.get_tool(name)
    resolver = getattr(tool, "resolve_timeout", None) if tool is not None else None
    if callable(resolver):
        resolved = resolver(args or {})
        if resolved is None:
            return None
        value = float(resolved)
        return value if value > 0 else None
    declared = getattr(tool, "timeout_seconds", None) if tool is not None else None
    if declared is not None:
        value = float(declared)
        return value if value > 0 else None
    return None


def tool_mutates(
    name: str,
    tool_registry: ToolRegistry | None = None,
    args: dict[str, object] | None = None,
) -> bool:
    """Whether a tool call mutates state according to tool-owned metadata."""
    return tool_side_effect_kind(name, tool_registry, args) in {
        "workspace", "external", "destructive"
    }


def tool_side_effect_kind(
    name: str,
    tool_registry: ToolRegistry | None = None,
    args: dict[str, object] | None = None,
) -> str:
    """Return a tool-owned side-effect class."""
    if tool_registry is not None:
        tool = tool_registry.get_tool(name)
        if tool is not None:
            return tool.get_side_effect_kind(args)
    return "none"


def tool_is_idempotent(
    name: str,
    tool_registry: ToolRegistry,
    args: dict[str, object] | None = None,
) -> bool:
    """Whether repeating the exact call is safe for loop guardrails/retries."""
    tool = tool_registry.get_tool(name)
    if tool is None:
        return False
    return tool.is_idempotent(args)
