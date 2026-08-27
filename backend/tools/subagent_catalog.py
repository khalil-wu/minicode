from __future__ import annotations

from typing import Any, Callable

BUILTIN_AGENT_TYPE_ORDER = [
    "general-purpose",
    "explore",
    "plan",
]
BUILTIN_AGENT_TYPES: set[str] = set(BUILTIN_AGENT_TYPE_ORDER)

_BUILTIN_AGENT_GUIDANCE = {
    "general-purpose": "Any multi-step task that does not fit a more specific type.",
    "explore": "Read-only investigation: locate code, answer 'where is X defined'.",
    "plan": "Design an implementation approach and return a step-by-step plan.",
}


def agent_type_description(
    agent_types: list[str],
    *,
    get_custom_agent: Callable[[str], Any | None] | None = None,
) -> str:
    """Describe each selectable subagent type for the model-facing schema.

    A bare enum tells the model nothing about when to pick a custom agent, so a
    user-authored ``agents/*.md`` would be undiscoverable in practice. Each line
    is rendered as ``- type: whenToUse``.
    """
    lines: list[str] = []
    for name in agent_types:
        guidance = _BUILTIN_AGENT_GUIDANCE.get(name, "")
        if not guidance and get_custom_agent is not None:
            try:
                definition = get_custom_agent(name)
            except Exception:
                definition = None
            guidance = str(getattr(definition, "description", "") or "").strip()
        lines.append(f"- {name}: {guidance}" if guidance else f"- {name}")
    return "Subagent type to delegate to. Available types:\n" + "\n".join(lines)


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
    """Resolve an agent type name exactly (cc AgentTool semantics).

    Lookups are case-sensitive against builtin and custom agent names; an
    unknown type raises with the available list instead of silently degrading
    to general-purpose.
    """
    agent_type = str(raw or "general-purpose").strip()
    if not agent_type:
        agent_type = "general-purpose"
    if agent_type == "general":
        agent_type = "general-purpose"
    if agent_type in BUILTIN_AGENT_TYPES:
        return agent_type
    if get_custom_agent is not None and get_custom_agent(agent_type):
        return agent_type
    raise ValueError(
        f"Agent type '{agent_type}' not found. Available agents: "
        f"{', '.join(BUILTIN_AGENT_TYPE_ORDER)} (plus discovered custom agents)"
    )
