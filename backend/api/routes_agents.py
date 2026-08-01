"""Custom agent (subagent role) CRUD routes — backs the Agent editor UI."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel

from backend.services.agent_editor_service import (
    AgentEditorServiceError,
    AgentUpsertPayload,
    delete_agent,
    list_agents,
    upsert_agent,
)

router = APIRouter()


class AgentUpsertRequest(BaseModel):
    name: str
    description: str = ""
    prompt: str = ""
    model: str = ""
    tools: list[str] = []
    disallowed_tools: list[str] = []


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


@router.get("/api/agents")
async def list_agents_api(response: Response) -> dict[str, Any]:
    """List all discovered custom agents."""
    _no_store(response)
    try:
        return list_agents()
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
                tools=request.tools,
                disallowed_tools=request.disallowed_tools,
            )
        )
    except AgentEditorServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/api/agents/{name}")
async def delete_agent_api(name: str, response: Response) -> dict[str, Any]:
    """Delete a user-defined custom agent."""
    _no_store(response)
    try:
        return delete_agent(name)
    except AgentEditorServiceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
