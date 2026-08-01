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
        try:
            resolved = resolver(args or {})
            if resolved is not None:
                value = float(resolved)
                return value if value > 0 else None
        except (TypeError, ValueError):
            pass
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
    if tool_registry is not None:
        tool = tool_registry.get_tool(name)
        if tool is not None:
            # The default BaseTool implementation already maps legacy
            # mutates_* flags. Do not re-apply those flags after an
            # argument-sensitive override classified this invocation.
            side_effect_kind = tool_side_effect_kind(name, tool_registry, args)
            return side_effect_kind in {"workspace", "external", "destructive"}
    return False


def tool_side_effect_kind(
    name: str,
    tool_registry: ToolRegistry | None = None,
    args: dict[str, object] | None = None,
) -> str:
    """Return a tool-owned side-effect class."""
    if tool_registry is not None:
        tool = tool_registry.get_tool(name)
        if tool is not None:
            get_kind = getattr(tool, "get_side_effect_kind", None)
            if callable(get_kind):
                try:
                    return str(get_kind(args)).strip().lower() or "none"
                except Exception:
                    pass
            if getattr(tool, "destructive", False):
                return "destructive"
            if getattr(tool, "mutates_external_state", False):
                return "external"
            if getattr(tool, "mutates_workspace", False):
                return "workspace"
            return "none"
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
    is_idempotent = getattr(tool, "is_idempotent", None)
    if callable(is_idempotent):
        try:
            return bool(is_idempotent(args))
        except Exception:
            return bool(getattr(tool, "read_only", False)) and not tool_mutates(name, tool_registry, args)
    return bool(getattr(tool, "read_only", False)) and not tool_mutates(name, tool_registry, args)
