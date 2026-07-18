from __future__ import annotations

from typing import Any, Callable

BUILTIN_AGENT_TYPE_ORDER = [
    "general-purpose",
    "explore",
    "plan",
    "implement",
    "verification",
]
BUILTIN_AGENT_TYPES: set[str] = set(BUILTIN_AGENT_TYPE_ORDER)


def available_agent_types(
    discover_custom_agents: Callable[[], dict[str, Any]] | None = None,
) -> list[str]:
    """Return built-in plus discovered custom subagent types.

    ``discover_custom_agents`` is injectable so tests and future module splits can
    patch one stable hook instead of reaching into whichever file owns TaskTool.
    """
    custom: list[str] = []
    if discover_custom_agents is not None:
        try:
            custom = sorted(
                name
                for name in discover_custom_agents().keys()
                if name and name not in BUILTIN_AGENT_TYPES
            )
        except Exception:
            custom = []
    return [*BUILTIN_AGENT_TYPE_ORDER, *custom]


def normalize_agent_type(
    raw: str | None,
    *,
    get_custom_agent: Callable[[str], Any | None] | None = None,
) -> str:
    agent_type = str(raw or "general-purpose").strip().lower()
    if agent_type == "general":
        agent_type = "general-purpose"
    if agent_type in BUILTIN_AGENT_TYPES:
        return agent_type
    if get_custom_agent is not None and get_custom_agent(agent_type):
        return agent_type
    return "general-purpose"
