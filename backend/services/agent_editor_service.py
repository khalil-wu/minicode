from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from pathlib import Path

from backend.workspace.state import get_explicit_active_workspace_root

from backend.agents.loader import (
    AgentDefinition,
    EditableAgentSource,
    delete_custom_agent,
    discover_agent_definitions,
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
    effort: str = ""
    tools: list[str] | None = None
    disallowed_tools: list[str] | None = None
    source: str = ""
    location: str = ""
    source_path: str = ""


def serialize_agent(
    agent: AgentDefinition,
    *,
    active_source_path: Path | None = None,
) -> dict[str, Any]:
    source_path = agent.source_path.resolve() if agent.source_path else None
    source = agent.source
    editable = source in {"user", "project"}
    return {
        "name": agent.name,
        "description": agent.description,
        "prompt": agent.prompt,
        "model": agent.model,
        "effort": agent.effort,
        "tools": agent.tools,
        "disallowed_tools": agent.disallowed_tools,
        "source_path": str(source_path) if source_path else None,
        "filename": agent.filename or (source_path.stem if source_path else agent.name),
        "source": source,
        "location": source,
        "editable": editable,
        "deletable": editable,
        # Managed agents have the highest precedence, so promising a
        # project "override" would be false. They remain view-only.
        "can_override": False,
        "active": bool(
            source_path is not None
            and active_source_path is not None
            and source_path == active_source_path.resolve()
        ),
    }


def _active_workspace(workspace_root: str | Path | None = None) -> Path | None:
    requested = str(workspace_root or "").strip()
    if requested:
        candidate = Path(requested).expanduser().resolve()
        if not candidate.exists() or not candidate.is_dir():
            raise AgentEditorServiceError("workspace_root must be an existing directory")
        return candidate
    return get_explicit_active_workspace_root()


def _editable_source(
    value: str,
    *,
    default: str = "project",
) -> EditableAgentSource:
    source = str(value or default).strip() or default
    if source == "user":
        return "user"
    if source == "project":
        return "project"
    raise AgentEditorServiceError("Only user and project Agent files are editable.")


def list_agents(*, workspace_root: str | Path | None = None) -> dict[str, Any]:
    workspace_root = (
        _active_workspace()
        if workspace_root is None
        else _active_workspace(workspace_root)
    )
    definitions = discover_agent_definitions(workspace_root)
    active_agents = discover_agents(workspace_root)
    return {
        "agents": [
            serialize_agent(
                agent,
                active_source_path=(
                    active_agents[agent.name].source_path
                    if agent.name in active_agents
                    else None
                ),
            )
            for agent in definitions
        ]
    }


def upsert_agent(
    payload: AgentUpsertPayload,
    *,
    workspace_root: str | Path | None = None,
) -> dict[str, Any]:
    workspace_root = (
        _active_workspace()
        if workspace_root is None
        else _active_workspace(workspace_root)
    )
    source = _editable_source(
        payload.source if payload.source_path else payload.location,
    )
    try:
        agent = save_custom_agent(
            payload.name,
            description=payload.description,
            prompt=payload.prompt,
            model=payload.model,
            effort=payload.effort,
            tools=payload.tools or [],
            disallowed_tools=payload.disallowed_tools or [],
            workspace_root=workspace_root,
            source=source,
            source_path=payload.source_path or None,
        )
    except ValueError as exc:
        raise AgentEditorServiceError(str(exc)) from exc
    return {"agent": serialize_agent(agent, active_source_path=agent.source_path)}


def delete_agent(
    name: str,
    *,
    source: str = "",
    source_path: str = "",
    workspace_root: str | Path | None = None,
) -> dict[str, Any]:
    clean_name = str(name or "")
    clean_source = _editable_source(source) if source else None
    deleted = delete_custom_agent(
        clean_name,
        (
            _active_workspace()
            if workspace_root is None
            else _active_workspace(workspace_root)
        ),
        source=clean_source,
        source_path=source_path or None,
    )
    if not deleted:
        raise AgentEditorServiceError("Agent not found or not deletable")
    return {"deleted": True, "name": clean_name}
