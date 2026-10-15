from __future__ import annotations

from typing import Any, Callable

BUILTIN_AGENT_TYPE_ORDER = [
    "default",
    "explorer",
    "worker",
    "general-purpose",
    "explore",
    "plan",
]
BUILTIN_AGENT_TYPES: set[str] = set(BUILTIN_AGENT_TYPE_ORDER)

_BUILTIN_AGENT_GUIDANCE = {
    "default": "Default agent.",
    "explorer": """Use `explorer` for specific codebase questions.
Explorers are fast and authoritative.
They must be used to ask specific, well-scoped questions on the codebase.
Rules:
- In order to avoid redundant work, you should avoid exploring the same problem that explorers have already covered. Typically, you should trust the explorer results without additional verification. You are still allowed to inspect the code yourself to gain the needed context!
- You are encouraged to spawn up multiple explorers in parallel when you have multiple distinct questions to ask about the codebase that can be answered independently. This allows you to get more information faster without waiting for one question to finish before asking the next. While waiting for the explorer results, you can continue working on other local tasks that do not depend on those results. This parallelism is a key advantage of delegation, so use it whenever you have multiple questions to ask.
- Reuse existing explorers for related questions.""",
    "worker": """Use for execution and production work.
Typical tasks:
- Implement part of a feature
- Fix tests or bugs
- Split large refactors into independent chunks
Rules:
- Explicitly assign **ownership** of the task (files / responsibility). When the subtask involves code changes, you should clearly specify which files or modules the worker is responsible for. This helps avoid merge conflicts and ensures accountability. For example, you can say "Worker 1 is responsible for updating the authentication module, while Worker 2 will handle the database layer." By defining clear ownership, you can delegate more effectively and reduce coordination overhead.
- Always tell workers they are **not alone in the codebase**, and they should not revert the edits made by others, and they should adjust their implementation to accommodate the changes made by others. This is important because there may be multiple workers making changes in parallel, and they need to be aware of each other's work to avoid conflicts and ensure a cohesive final product.""",
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
        definition = get_custom_agent(name) if get_custom_agent is not None else None
        guidance = str(getattr(definition, "description", "") or "").strip() if definition is not None else _BUILTIN_AGENT_GUIDANCE.get(name, "")
        if definition is not None:
            guidance = str(getattr(definition, "description", "") or "").strip()
            from backend.agents.source_roles import SourceAgentRole

            if isinstance(definition, SourceAgentRole):
                if definition.model:
                    guidance += f"\n- This role's model is set to `{definition.model}` and cannot be changed."
                if definition.effort:
                    guidance += f"\n- This role's reasoning effort is set to `{definition.effort}` and cannot be changed."
        lines.append(f"- {name}: {guidance}" if guidance else f"- {name}")
    return "Subagent type to delegate to. Available types:\n" + "\n".join(lines)


def available_agent_types(
    discover_custom_agents: Callable[[], dict[str, Any]] | None = None,
    *, include_legacy: bool = True,
) -> list[str]:
    """Return built-in plus discovered custom subagent types.

    ``discover_custom_agents`` is injectable so tests and future module splits can
    patch one stable hook instead of reaching into whichever file owns TaskTool.
    """
    custom: list[str] = []
    builtin = BUILTIN_AGENT_TYPE_ORDER if include_legacy else BUILTIN_AGENT_TYPE_ORDER[:3]
    if discover_custom_agents is not None:
        custom = sorted(
            name
            for name in discover_custom_agents().keys()
            if name and name not in builtin
        )
    return [*builtin, *custom]


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
    agent_type = str(raw or "default").strip()
    if not agent_type:
        agent_type = "default"
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
