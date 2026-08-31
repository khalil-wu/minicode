"""Custom agent (subagent role) CRUD routes — backs the Agent editor UI."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field

from backend.llm.model_selection import (
    default_model_thinking_level,
    model_thinking_levels,
)
from backend.workspace.state import get_explicit_active_workspace_root

from . import _state

from backend.services.agent_editor_service import (
    AgentEditorServiceError,
    AgentUpsertPayload,
    delete_agent,
    list_agents,
    upsert_agent,
)

router = APIRouter()


def _live_agent_model_catalog(workspace_root_override: str = "") -> list[dict[str, Any]]:
    """Project the active MiniCode model catalog for the workspace."""

    workspace_root = (
        Path(workspace_root_override).expanduser().resolve()
        if str(workspace_root_override or "").strip()
        else get_explicit_active_workspace_root()
    )
    iter_sessions = getattr(_state.ws_manager, "iter_sessions", None)
    sessions = list(iter_sessions()) if callable(iter_sessions) else []
    for session in reversed(sessions):
        try:
            if not session.is_connected:
                continue
            session_workspace = session.session_lifecycle.workspace_root_for_conversation()
            if (
                workspace_root is not None
                and session_workspace is not None
                and session_workspace.resolve() != workspace_root.resolve()
            ):
                continue
            resolver = getattr(session, "_model_runtime_for_conversation", None)
            runtime = (
                resolver(getattr(session, "active_conversation_id", None))
                if callable(resolver)
                else None
            )
            if runtime is None or not bool(getattr(runtime, "active", True)):
                continue
            catalog: list[dict[str, Any]] = []
            for model in runtime.get_available_snapshot():
                levels = model_thinking_levels(model)
                if (
                    model.api == "anthropic-messages"
                    and bool(getattr(model, "reasoning", False))
                ):
                    # MiniCode's concrete Messages adapter exposes one configured
                    # budget, so its faithful surface is off/high.
                    levels = ("off", "high")
                provider = runtime.get_provider(model.provider)
                catalog.append(
                    {
                        "provider": model.provider,
                        "provider_name": (
                            str(getattr(provider, "name", "") or model.provider)
                        ),
                        "model": model.id,
                        "model_name": str(getattr(model, "name", "") or model.id),
                        "reasoning_effort_levels": list(levels),
                        "default_reasoning_effort": default_model_thinking_level(
                            model,
                            levels,
                        ),
                    }
                )
            return catalog
        except RuntimeError:
            # A MiniCode extension reload retires the old conversation-owned
            # ModelRuntime atomically. If retirement lands between the snapshot
            # and provider projection, discard that generation and try another
            # connected session rather than turning the Agent list into a 500.
            continue
    return []


class AgentUpsertRequest(BaseModel):
    name: str
    description: str = ""
    prompt: str = ""
    model: str = ""
    effort: str = ""
    tools: list[str] = Field(default_factory=list)
    disallowed_tools: list[str] = Field(default_factory=list)
    source: str = ""
    location: str = ""
    source_path: str = ""
    workspace_root: str = ""


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


@router.get("/api/agents")
async def list_agents_api(
    response: Response,
    workspace_root: str = "",
) -> dict[str, Any]:
    """List all discovered custom agents."""
    _no_store(response)
    try:
        payload = list_agents(workspace_root=workspace_root)
        payload["model_catalog"] = _live_agent_model_catalog(workspace_root)
        return payload
    except AgentEditorServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/agents")
async def upsert_agent_api(request: AgentUpsertRequest, response: Response) -> dict[str, Any]:
    """Create or overwrite a custom agent."""
    _no_store(response)
    try:
        return upsert_agent(
            AgentUpsertPayload(
                name=request.name,
                description=request.description,
                prompt=request.prompt,
                model=request.model,
                effort=request.effort,
                tools=request.tools,
                disallowed_tools=request.disallowed_tools,
                source=request.source,
                location=request.location,
                source_path=request.source_path,
            ),
            workspace_root=request.workspace_root,
        )
    except AgentEditorServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/api/agents/{name}")
async def delete_agent_api(
    name: str,
    response: Response,
    source: str = "",
    source_path: str = "",
    workspace_root: str = "",
) -> dict[str, Any]:
    """Delete a user-defined custom agent."""
    _no_store(response)
    try:
        return delete_agent(
            name,
            source=source,
            source_path=source_path,
            workspace_root=workspace_root,
        )
    except AgentEditorServiceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
