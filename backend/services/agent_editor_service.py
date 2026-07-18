from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from backend.agents.loader import (
    AgentDefinition,
    delete_custom_agent,
    discover_agents,
    save_custom_agent,
)


class AgentEditorServiceError(ValueError):
    """User-correctable agent editor operation failure."""


@dataclass(frozen=True)
class AgentUpsertPayload:
    name: str
    description: str = ""
    prompt: str = ""
    model: str = ""
    tools: list[str] | None = None
    disallowed_tools: list[str] | None = None


def serialize_agent(agent: AgentDefinition) -> dict[str, Any]:
    return {
        "name": agent.name,
        "description": agent.description,
        "prompt": agent.prompt,
        "model": agent.model,
        "tools": agent.tools,
        "disallowed_tools": agent.disallowed_tools,
        "source_path": str(agent.source_path) if agent.source_path else None,
    }


def list_agents() -> dict[str, Any]:
    agents = discover_agents()
    return {"agents": [serialize_agent(agent) for agent in agents.values()]}


def upsert_agent(payload: AgentUpsertPayload) -> dict[str, Any]:
    try:
        agent = save_custom_agent(
            payload.name,
            description=payload.description,
            prompt=payload.prompt,
            model=payload.model,
            tools=payload.tools or [],
            disallowed_tools=payload.disallowed_tools or [],
        )
    except ValueError as exc:
        raise AgentEditorServiceError(str(exc)) from exc
    return {"agent": serialize_agent(agent)}


def delete_agent(name: str) -> dict[str, Any]:
    clean_name = str(name or "")
    deleted = delete_custom_agent(clean_name)
    if not deleted:
        raise AgentEditorServiceError("Agent not found or not deletable")
    return {"deleted": True, "name": clean_name}
