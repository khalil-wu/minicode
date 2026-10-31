"""Workspace-scoped agent and guideline routes honour the workspace trust gate."""

import pytest
from fastapi import HTTPException, Response

from backend.api import routes_agents, routes_health


@pytest.mark.asyncio
async def test_untrusted_workspace_root_is_rejected_by_agent_routes(monkeypatch, tmp_path):
    monkeypatch.setattr("backend.workspace.trust.is_workspace_trusted", lambda _root: False)
    request = routes_agents.AgentUpsertRequest(name="helper", workspace_root=str(tmp_path))
    for call in (
        lambda: routes_agents.list_agents_api(Response(), workspace_root=str(tmp_path)),
        lambda: routes_agents.upsert_agent_api(request, Response()),
        lambda: routes_agents.delete_agent_api("helper", Response(), workspace_root=str(tmp_path)),
    ):
        with pytest.raises(HTTPException) as caught:
            await call()
        assert caught.value.status_code == 403


@pytest.mark.asyncio
async def test_untrusted_workspace_dir_is_rejected_by_guidelines_route(monkeypatch, tmp_path):
    monkeypatch.setattr("backend.workspace.trust.is_workspace_trusted", lambda _root: False)
    with pytest.raises(HTTPException) as caught:
        await routes_health.project_guidelines_status(Response(), workspace_dir=str(tmp_path))
    assert caught.value.status_code == 403


@pytest.mark.asyncio
async def test_trusted_workspace_root_still_lists_agents(monkeypatch, tmp_path):
    monkeypatch.setattr("backend.workspace.trust.is_workspace_trusted", lambda _root: True)
    payload = await routes_agents.list_agents_api(Response(), workspace_root=str(tmp_path))
    assert isinstance(payload["agents"], list) and payload["model_catalog"] == []
