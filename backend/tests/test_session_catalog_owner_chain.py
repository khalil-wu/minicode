from types import SimpleNamespace

import pytest

from backend.api import routes_agents
from backend.ws.handlers.session import handle_session_status_inspect


@pytest.mark.parametrize("target, session_scope, expected", [
    ("project", None, False), (None, "project", False), ("project", "project", True),
])
def test_agent_model_catalog_matches_workspace_including_projectless(tmp_path, monkeypatch, target, session_scope, expected):
    project = tmp_path / "project"
    model = SimpleNamespace(id="owned-model", provider="owned-provider", api="openai-responses", reasoning=False)
    runtime = SimpleNamespace(active=True, get_available_snapshot=lambda: [model],
        get_provider=lambda provider: SimpleNamespace(name="Owned provider"))
    session = SimpleNamespace(is_connected=True, active_conversation_id="conversation",
        session_lifecycle=SimpleNamespace(workspace_root_for_conversation=lambda: project if session_scope else None),
        _model_runtime_for_conversation=lambda conversation_id: runtime)
    monkeypatch.setattr(routes_agents._state, "ws_manager", SimpleNamespace(iter_sessions=lambda: [session]))
    catalog = routes_agents._live_agent_model_catalog(str(project) if target else "")
    assert [item["model"] for item in catalog] == (["owned-model"] if expected else [])


@pytest.mark.asyncio
async def test_session_status_uses_its_own_mcp_manager(monkeypatch):
    monkeypatch.setattr("backend.api.routes_health.get_mcp_status", lambda: [{"name": "other-workspace", "status": "connected"}])
    results = []

    async def emit(*args, **kwargs):
        results.append(kwargs["data"])

    session = SimpleNamespace(session_id="owner", selected_model="owned-model",
        permission_context=SimpleNamespace(mode="confirm"),
        mcp_manager=SimpleNamespace(get_all_status=lambda: []),
        runtime_snapshot=lambda: {"task_summary": {}}, emit_command_result=emit)
    await handle_session_status_inspect(session, {})
    assert results[0]["mcp"] == []
