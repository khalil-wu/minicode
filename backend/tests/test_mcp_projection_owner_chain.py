import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.ws.handlers.mcp import _run_mcp_server_command
from backend.ws.handler import WebSocketSession
from backend.ws.mcp_projection import send_mcp_projection


@pytest.mark.asyncio
async def test_mcp_command_keeps_its_manager_but_does_not_publish_into_new_workspace(tmp_path, monkeypatch):
    manager_a = SimpleNamespace(workspace_root=tmp_path / "A")
    manager_b = SimpleNamespace(workspace_root=tmp_path / "B")
    reload_managers = AsyncMock()
    monkeypatch.setattr("backend.api._state.bootstrap", SimpleNamespace(reload_mcp_managers=reload_managers))
    session = SimpleNamespace(
        active_conversation_id="A", mcp_manager=manager_a,
        send_payload=AsyncMock(), send_event=AsyncMock(),
        refresh_tool_registry_if_mcp_changed=lambda **_kwargs: True,
    )
    started, finish = asyncio.Event(), asyncio.Event()
    invoked = []

    async def invoke(manager, _data):
        invoked.append(manager)
        started.set()
        await finish.wait()
        return [{"name": "shared", "status": "connected", "project_workspace": str(manager_a.workspace_root)}]

    task = asyncio.create_task(_run_mcp_server_command(session, {"name": "shared"}, command="mcp.update", invoke=invoke, reload_other_managers=True))
    await started.wait()
    session.active_conversation_id = "B"
    session.mcp_manager = manager_b
    finish.set()
    await task
    assert invoked == [manager_a]
    reload_managers.assert_awaited_once_with(exclude=manager_a)
    session.send_payload.assert_not_awaited()
    assert session.send_event.await_args.args[0].data["command"] == "mcp.update"


@pytest.mark.asyncio
async def test_mcp_projection_carries_scope_and_checks_manager_at_each_emit(tmp_path):
    manager = SimpleNamespace(workspace_root=tmp_path)
    session = SimpleNamespace(active_conversation_id="owner", mcp_manager=manager, send_payload=AsyncMock())
    await send_mcp_projection(session, manager, {"type": "mcp_status", "servers": []})
    assert session.send_payload.await_args.args[0] == {
        "type": "mcp_status", "servers": [], "conversation_id": "owner", "workspace_root": str(tmp_path),
    }
    session.mcp_manager = SimpleNamespace(workspace_root=tmp_path / "other")
    await send_mcp_projection(session, manager, {"type": "mcp.progress", "server_name": "shared"})
    assert session.send_payload.await_count == 1


@pytest.mark.asyncio
async def test_deferred_mcp_command_rejects_a_stale_owner_before_invoking_service():
    invoke = AsyncMock()
    session = SimpleNamespace(active_conversation_id="B", send_event=AsyncMock())
    await _run_mcp_server_command(session, {"name": "shared", "conversation_id": "A"}, command="mcp.restart", invoke=invoke)
    invoke.assert_not_awaited()
    result = session.send_event.await_args.args[0]
    assert result.data["level"] == "error"
    assert "owner is stale" in result.data["message"]


@pytest.mark.asyncio
async def test_pending_workspace_manager_does_not_borrow_the_global_manager(monkeypatch):
    global_manager = object()
    monkeypatch.setattr("backend.api.routes_health.get_mcp_manager", lambda: global_manager)
    invoke = AsyncMock()
    session = SimpleNamespace(active_conversation_id="B", mcp_manager=None, send_event=AsyncMock())
    await _run_mcp_server_command(session, {"name": "shared"}, command="mcp.restart", invoke=invoke)
    invoke.assert_not_awaited()
    result = session.send_event.await_args.args[0]
    assert result.data["level"] == "error"
    assert "not available for this conversation yet" in result.data["message"]


def test_runtime_mcp_summary_reads_the_session_manager_only(monkeypatch):
    monkeypatch.setattr("backend.api.routes_health.get_mcp_status", lambda: [{"name": "other", "status": "connected"}])
    manager = SimpleNamespace(get_all_status=lambda: [{"name": "owner", "status": "error", "phase": "auth_required"}])
    summary = WebSocketSession._mcp_summary(SimpleNamespace(mcp_manager=manager))
    assert summary["auth_required"] == 1
    assert summary["connected"] == 0
    assert [server["name"] for server in summary["servers"]] == ["owner"]
    assert WebSocketSession._mcp_summary(SimpleNamespace(mcp_manager=None))["servers"] == []


@pytest.mark.asyncio
async def test_broadcast_rechecks_manager_after_status_delivery_switches_workspace(tmp_path, monkeypatch):
    from backend.main import _broadcast_mcp_status_change

    manager_a = SimpleNamespace(
        workspace_root=tmp_path / "A",
        get_all_status=lambda: [{"name": "shared", "status": "connected"}],
        get_server_lifecycle=lambda name: {"server_name": name, "phase": "connected"},
        get_server_progress=lambda name: {"server_name": name, "operation": "connect", "status": "completed"},
    )
    manager_b = SimpleNamespace(workspace_root=tmp_path / "B")
    session = SimpleNamespace(active_conversation_id="A", mcp_manager=manager_a, is_connected=True)
    received = []

    async def send_payload(payload, **_kwargs):
        received.append(payload)
        session.active_conversation_id = "B"
        session.mcp_manager = manager_b

    session.send_payload = send_payload
    monkeypatch.setattr("backend.api._state.ws_manager", SimpleNamespace(iter_sessions=lambda: [session]))
    await _broadcast_mcp_status_change("shared", object(), manager_a)
    assert [event["type"] for event in received] == ["mcp_status"]
    assert received[0]["conversation_id"] == "A"
    assert received[0]["workspace_root"] == str(manager_a.workspace_root)


@pytest.mark.asyncio
@pytest.mark.parametrize("superseded", [False, True])
async def test_workspace_activation_publishes_an_empty_ready_catalog_only_for_its_owner(tmp_path, monkeypatch, superseded):
    from backend.ws.session_lifecycle import SessionLifecycle
    from backend.services.workspace_service import WorkspaceActivationRequest

    started, ready = asyncio.Event(), asyncio.Event()
    manager = SimpleNamespace(workspace_root=tmp_path, get_all_status=lambda: [])

    async def begin_activation(_root):
        started.set()

        async def await_ready():
            await ready.wait()
            return manager

        return manager, asyncio.create_task(await_ready())

    session = SimpleNamespace(
        active_conversation_id="owner", mcp_manager=None, skill_manager=None,
        event_outbox=SimpleNamespace(client_command_id="activate"), command_registry=object(),
        send_payload=AsyncMock(), send_event=AsyncMock(), _run_cwd_changed_hook=AsyncMock(),
        refresh_tool_registry_if_mcp_changed=lambda **_kwargs: True,
    )
    lifecycle = SessionLifecycle(session)
    context = SimpleNamespace(root_path=tmp_path, initialize=AsyncMock(return_value={}))
    monkeypatch.setattr("backend.api._state.bootstrap", SimpleNamespace(begin_mcp_workspace_activation=begin_activation))
    monkeypatch.setattr("backend.services.workspace_service.parse_workspace_activation_request", lambda value: WorkspaceActivationRequest(value, tmp_path))
    monkeypatch.setattr("backend.services.workspace_service.create_workspace_context", lambda _root: context)
    monkeypatch.setattr("backend.services.workspace_service.record_recent_workspace_project", lambda *_args: None)
    monkeypatch.setattr("backend.services.workspace_service.list_workspace_recent_payload", lambda: {"type": "workspace.recent.list", "projects": []})
    monkeypatch.setattr("backend.services.mcp_service._read_current_config_data", lambda: {"servers": {}})
    monkeypatch.setattr("backend.commands.slash_commands.refresh_slash_commands", lambda _registry: None)
    monkeypatch.setattr(lifecycle, "send_runtime_capabilities", AsyncMock())
    monkeypatch.setattr(lifecycle, "restart_file_watcher", lambda _root: None)

    assert await lifecycle.activate_workspace_path(str(tmp_path))
    mcp_task = lifecycle.workspace_mcp_task
    context_task = lifecycle.workspace_context_task
    await started.wait()
    if superseded:
        lifecycle.workspace_generation += 1
        session.mcp_manager = SimpleNamespace(workspace_root=tmp_path / "other")
    ready.set()
    await mcp_task
    await context_task

    statuses = [call.args[0] for call in session.send_payload.await_args_list if call.args[0]["type"] == "mcp_status"]
    assert statuses == ([] if superseded else [{
        "type": "mcp_status", "servers": [], "conversation_id": "owner", "workspace_root": str(tmp_path),
    }])
