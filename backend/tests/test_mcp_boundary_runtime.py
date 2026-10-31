from __future__ import annotations

import asyncio
import json
import threading
import time
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import anyio
from fastapi.testclient import TestClient
from mcp import types
from mcp.shared.message import SessionMessage

from backend.api import _state
from backend.bootstrap.app import AppBootstrap
from backend.main import app
from backend.mcp import config_file
from backend.mcp.client import MCPClient, MCPServerCapabilities, _LifecycleClientSession
from backend.mcp.manager import MCPManagerReloadError, MCPServerConfig, MCPServerManager
from backend.services import mcp_service
from backend.tools.registry import ToolRegistry
from backend.ws.handlers import mcp as handlers
from backend.ws.command_scope import CommandScope, bind_execution_command_scope
from tests.test_conversations import _install_noop_llm, _receive_until_event


async def _resist_cancellation(release: asyncio.Event, cancelled: asyncio.Event):
    while not release.is_set():
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()


class _InventorySession:
    def __init__(self, manager=None):
        self.mcp_manager = manager or object()
        self.cleanup_tasks = set()
        self.events = []

    async def send_event(self, event):
        self.events.append(event.to_ws_message())


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled_externally", [False, True])
async def test_inventory_deadline_or_cancellation_retains_resistant_requests(monkeypatch, cancelled_externally):
    release, cancelled, started = asyncio.Event(), asyncio.Event(), asyncio.Event()
    cleanup = set()

    class Client:
        server_capabilities = MCPServerCapabilities(tools=True)

        async def list_tools(self):
            started.set()
            await _resist_cancellation(release, cancelled)
            return []

    manager = SimpleNamespace(get_client=lambda _: Client())
    monkeypatch.setattr(mcp_service, "MCP_REQUEST_TIMEOUT_SECONDS", .03)
    request = asyncio.create_task(mcp_service.list_mcp_inventory(manager, "docs", cleanup_tasks=cleanup))
    try:
        await asyncio.wait_for(started.wait(), 1)
        before = time.monotonic()
        if cancelled_externally:
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(request, .3)
        else:
            with pytest.raises(mcp_service.MCPInventoryServiceError) as failure:
                await asyncio.wait_for(request, .3)
            assert failure.value.code == "timeout"
        assert time.monotonic() - before < .3
        await asyncio.wait_for(cancelled.wait(), 1)
        assert len(cleanup) == 1 and not next(iter(cleanup)).done()
    finally:
        release.set()
        await asyncio.gather(request, *cleanup, return_exceptions=True)
        await asyncio.sleep(0)
    assert not cleanup


@pytest.mark.asyncio
async def test_inventory_cancel_fences_owner_and_suppresses_late_success(monkeypatch):
    started, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def inventory(*_args, **_kwargs):
        started.set()
        await _resist_cancellation(release, cancelled)
        return {"server_name": "docs"}

    monkeypatch.setattr(handlers, "list_mcp_inventory", inventory)
    session = _InventorySession()
    request = asyncio.create_task(handlers.handle_mcp_inventory_list(session, {"name": "docs", "operation_id": "same"}))
    try:
        await asyncio.wait_for(started.wait(), 1)
        await handlers.handle_mcp_inventory_cancel(session, {"name": "other", "operation_id": "same"})
        assert session.events[-1]["data"]["error_code"] == "operation_owner_mismatch"
        assert not cancelled.is_set()
        await asyncio.wait_for(handlers.handle_mcp_inventory_cancel(session, {"name": "docs", "operation_id": "same"}), .3)
        await asyncio.wait_for(request, .3)
        cancellation = next(event for event in session.events if event.get("command") == "mcp.inventory.cancel" and event["level"] == "info")
        assert cancellation["data"]["cleanup_pending"] is True
        assert "same" in session._mcp_inventory_tasks and session.cleanup_tasks
        await handlers.handle_mcp_inventory_list(session, {"name": "docs", "operation_id": "same"})
        assert session.events[-1]["data"]["error_code"] == "operation_conflict"
    finally:
        release.set()
        await asyncio.gather(request, *session.cleanup_tasks, return_exceptions=True)
        await asyncio.sleep(0)
    assert session._mcp_inventory_tasks == {}
    assert not any(event.get("command") == "mcp.inventory.list" and event["level"] != "error" for event in session.events)


@pytest.mark.asyncio
async def test_same_operation_id_in_another_window_is_independent(monkeypatch):
    first, second = _InventorySession(), _InventorySession()
    entered = {first.mcp_manager: asyncio.Event(), second.mcp_manager: asyncio.Event()}
    release = {first.mcp_manager: asyncio.Event(), second.mcp_manager: asyncio.Event()}

    async def inventory(manager, _name, **_kwargs):
        entered[manager].set()
        await release[manager].wait()
        return {"server_name": "docs"}

    monkeypatch.setattr(handlers, "list_mcp_inventory", inventory)
    requests = [asyncio.create_task(handlers.handle_mcp_inventory_list(session, {"name": "docs", "operation_id": "same"}))
                for session in (first, second)]
    try:
        await asyncio.gather(*(event.wait() for event in entered.values()))
        await handlers.handle_mcp_inventory_cancel(first, {"name": "docs", "operation_id": "same"})
        await requests[0]
        assert not requests[1].done()
        release[second.mcp_manager].set()
        await requests[1]
        assert second.events[-1]["data"]["inventory"]["server_name"] == "docs"
        assert not first._mcp_inventory_tasks and not second._mcp_inventory_tasks
    finally:
        for event in release.values():
            event.set()
        await asyncio.gather(*requests, *first.cleanup_tasks, *second.cleanup_tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_reused_operation_id_is_not_changed_by_the_cancelled_queries_late_cleanup():
    first_started, second_started = asyncio.Event(), asyncio.Event()
    first_release, second_release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Client:
        server_capabilities = MCPServerCapabilities(tools=True)
        calls = 0

        async def list_tools(self):
            self.calls += 1
            if self.calls == 1:
                first_started.set()
                await _resist_cancellation(first_release, cancelled)
            else:
                second_started.set()
                await second_release.wait()
            return []

    client = Client()
    session = _InventorySession(SimpleNamespace(get_client=lambda _: client))
    first = asyncio.create_task(handlers.handle_mcp_inventory_list(session, {"name": "docs", "operation_id": "reuse"}))
    second = None
    try:
        await asyncio.wait_for(first_started.wait(), 1)
        await handlers.handle_mcp_inventory_cancel(session, {"name": "docs", "operation_id": "reuse"})
        await asyncio.wait_for(first, .3)
        await asyncio.sleep(0)
        assert session.cleanup_tasks and not session._mcp_inventory_tasks
        second = asyncio.create_task(handlers.handle_mcp_inventory_list(session, {"name": "docs", "operation_id": "reuse"}))
        await asyncio.wait_for(second_started.wait(), 1)
        operation = session._mcp_inventory_tasks["reuse"]
        first_release.set()
        await asyncio.sleep(.01)
        assert not second.done() and session._mcp_inventory_tasks["reuse"] is operation
        second_release.set()
        await second
        results = [event for event in session.events if event.get("command") == "mcp.inventory.list"]
        assert [event["level"] for event in results] == ["error", "info"]
    finally:
        first_release.set()
        second_release.set()
        await asyncio.gather(first, *([second] if second is not None else []), *session.cleanup_tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_client_close_accounts_for_a_running_inventory_request(monkeypatch):
    release, cancelled, started = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Session:
        async def list_resources(self, **_kwargs):
            started.set()
            await _resist_cancellation(release, cancelled)
            return types.ListResourcesResult(resources=[])

    client = MCPClient("inventory-close")
    client._connected, client._session = True, Session()
    client._server_capabilities = MCPServerCapabilities(resources=True)
    monkeypatch.setattr("backend.mcp.client.CANCELLATION_DRAIN_TIMEOUT_SECONDS", .02)
    request = asyncio.create_task(client.list_resources())
    try:
        await asyncio.wait_for(started.wait(), 1)
        assert request in client.pending_requests()
        assert not await asyncio.wait_for(client.close(), .3)
        assert request in client.pending_requests() and client.cleanup_status["pending"]
    finally:
        release.set()
        await asyncio.gather(request, return_exceptions=True)
        assert await client.finish_pending_cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["cancel", "timeout"])
async def test_ordinary_tool_cancel_or_timeout_notifies_the_original_sdk_request_without_waiting_for_pipe(outcome):
    server_write, client_read = anyio.create_memory_object_stream[SessionMessage](0)
    client_write, server_read = anyio.create_memory_object_stream[SessionMessage](0)
    client = MCPClient("ordinary-server", tool_timeout=.03 if outcome == "timeout" else 30)
    cancel = asyncio.Event()
    async with _LifecycleClientSession(client_read, client_write, transport_closed=asyncio.Event(),
                                      cancellation_tasks=client._active_request_tasks) as sdk:
        client._session, client._connected = sdk, True
        request = asyncio.create_task(client.call_tool("pending", request_owner={"cancel_event": cancel}))
        wire_request = await server_read.receive()
        try:
            if outcome == "cancel":
                cancel.set()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(request, .3)
            else:
                result = await asyncio.wait_for(request, .3)
                assert result.is_error and "timed out" in result.text
            assert client.pending_requests()
            notification = await asyncio.wait_for(server_read.receive(), .3)
            assert notification.message.root.method == "notifications/cancelled"
            assert notification.message.root.params["requestId"] == wire_request.message.root.id
            await asyncio.gather(*client.pending_requests(), return_exceptions=True)
            await asyncio.sleep(0)
            assert not client.pending_requests()
        finally:
            request.cancel()
            for task in tuple(client._active_request_tasks):
                task.cancel()
            await asyncio.gather(request, *client._active_request_tasks, return_exceptions=True)
    await server_write.aclose()
    await server_read.aclose()


@pytest.mark.asyncio
async def test_real_stdio_tool_receives_cancellation_and_keeps_its_connection_usable(tmp_path):
    script = tmp_path / "cancel_server.py"
    script.write_text('''import asyncio, sys
from pathlib import Path
from mcp.server.fastmcp import FastMCP
server = FastMCP("cancel-boundary")
root = Path(sys.argv[1])
@server.tool()
async def slow() -> str:
    (root / "started").write_text("started")
    try:
        await asyncio.sleep(3600)
        return "late"
    finally:
        (root / "cancelled").write_text("cancelled")
@server.tool()
def fast() -> str:
    return "connection-ready"
server.run(transport="stdio")
''', encoding="utf-8")
    client = MCPClient("real-cancel", command=sys.executable, args=[str(script), str(tmp_path)], cwd=str(tmp_path))
    request = None
    try:
        await asyncio.wait_for(client.connect(), 10)
        cancel = asyncio.Event()
        request = asyncio.create_task(client.call_tool("slow", request_owner={"cancel_event": cancel}))
        async with asyncio.timeout(3):
            while not (tmp_path / "started").exists():
                await asyncio.sleep(.01)
        cancel.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(request, 1)
        async with asyncio.timeout(3):
            while not (tmp_path / "cancelled").exists():
                await asyncio.sleep(.01)
        result = await asyncio.wait_for(client.call_tool("fast"), 3)
        assert not result.is_error and "connection-ready" in result.text
    finally:
        if request is not None:
            request.cancel()
            await asyncio.gather(request, return_exceptions=True)
        assert await client.close()


@pytest.mark.asyncio
async def test_cancelled_response_ids_are_exact_and_unrelated_protocol_errors_are_preserved():
    server_write, client_read = anyio.create_memory_object_stream[SessionMessage](2)
    client_write, server_read = anyio.create_memory_object_stream[SessionMessage](2)
    client = MCPClient("request-id-owner")
    async with _LifecycleClientSession(client_read, client_write, transport_closed=asyncio.Event(),
                                      cancellation_tasks=client._active_request_tasks,
                                      message_handler=client._sdk_message_handler) as sdk:
        client._session, client._connected = sdk, True
        request = asyncio.create_task(client.call_tool("pending"))
        wire = await server_read.receive()
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        await server_read.receive()
        await sdk._handle_response(SessionMessage(types.JSONRPCMessage(types.JSONRPCError(
            jsonrpc="2.0", id=wire.message.root.id, error=types.ErrorData(code=0, message="request cancelled")))))
        assert client.connected
        assert not sdk._cancelled_tool_request_ids
        with pytest.raises(ConnectionError, match="unknown request ID"):
            await sdk._handle_response(SessionMessage(types.JSONRPCMessage(types.JSONRPCResponse(
                jsonrpc="2.0", id=999, result={"content": []}))))
        assert not client.connected
    await server_write.aclose()
    await server_read.aclose()


@pytest.mark.timeout(45)
def test_websocket_cancel_remains_responsive_and_never_projects_late_inventory(tmp_path, monkeypatch):
    _install_noop_llm(monkeypatch)
    monkeypatch.setattr("backend.main.CONVERSATION_DATA_DIR", tmp_path / "conversations", raising=False)
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", tmp_path / "conversations", raising=False)
    started, release_holder = threading.Event(), {}

    async def inventory(*_args, **_kwargs):
        release = release_holder["release"] = asyncio.Event()
        started.set()
        await _resist_cancellation(release, asyncio.Event())
        return {"server_name": "docs", "tools": [], "resources": [], "resource_templates": [], "prompts": [], "empty": True}

    monkeypatch.setattr(handlers, "list_mcp_inventory", inventory)
    with TestClient(app) as client:
        with client.websocket_connect("/ws?session_id=mcp-boundary") as socket:
            _receive_until_event(socket, "llm.model.updated")
            session = _state.ws_manager.get_session("mcp-boundary")
            socket.send_json({"type": "mcp.inventory.list", "name": "docs", "operation_id": "resistant", "client_command_id": "list-request"})
            assert started.wait(5)
            socket.send_json({"type": "mcp.inventory.cancel", "name": "docs", "operation_id": "resistant", "client_command_id": "cancel-request"})
            outcomes = {}
            while len(outcomes) < 2:
                event = socket.receive_json()
                if event.get("type") == "command.result" and event.get("command", "").startswith("mcp.inventory."):
                    outcomes[event["command"]] = event
            assert outcomes["mcp.inventory.list"]["data"]["error_code"] == "cancelled"
            assert outcomes["mcp.inventory.cancel"]["data"]["cleanup_pending"] is True
            socket.send_json({"type": "ping"})
            _receive_until_event(socket, "pong", max_attempts=100)
            client.portal.call(release_holder["release"].set)
            socket.send_json({"type": "ping"})
            while True:
                event = socket.receive_json()
                assert not (event.get("command") == "mcp.inventory.list" and event.get("level") != "error")
                if event.get("type") == "pong":
                    break
            assert not session._mcp_inventory_tasks


class _CommandSession:
    def __init__(self, manager):
        self.mcp_manager = manager
        self.active_conversation_id = "owner"
        self.events, self.payloads = [], []

    async def send_event(self, event):
        self.events.append(event.to_ws_message())

    async def send_payload(self, payload, **_kwargs):
        self.payloads.append(payload)

    def refresh_tool_registry_if_mcp_changed(self, **_kwargs):
        return True


def _actual_config(tmp_path, monkeypatch, original):
    path = tmp_path / ".mcp.json"
    path.write_text(json.dumps(original), encoding="utf-8")
    read, write = config_file.read_mcp_config, config_file.write_mcp_config
    monkeypatch.setattr(mcp_service, "MCP_CONFIG_FILE", path)
    monkeypatch.setattr(config_file, "read_mcp_config", lambda: read(path))
    monkeypatch.setattr(config_file, "write_mcp_config", lambda content, _path: write(content, path))
    return path


async def _noop_hook(**_kwargs):
    return None


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["mcp.add", "mcp.update", "mcp.toggle", "mcp.remove"])
async def test_saved_configuration_reload_failure_has_an_accurate_receipt(tmp_path, monkeypatch, command):
    original = {"servers": {} if command == "mcp.add" else {"docs": {"transport": "stdio", "command": "python"}}}
    path = _actual_config(tmp_path, monkeypatch, original)

    class Manager:
        workspace_root = tmp_path

        async def reload_config(self):
            raise OSError("actual reload failure")

        def get_all_status(self):
            return []

    manager = Manager()
    session = _CommandSession(manager)
    monkeypatch.setattr(_state, "bootstrap", None)
    data = {"name": "docs", "transport": "stdio", "command": "node"}
    operations = {
        "mcp.add": lambda owner, payload: mcp_service.add_mcp_server(owner, payload, config_change_hook=_noop_hook),
        "mcp.update": lambda owner, payload: mcp_service.update_mcp_server(owner, payload, config_change_hook=_noop_hook),
        "mcp.toggle": lambda owner, payload: mcp_service.toggle_mcp_server(owner, payload["name"], False, config_change_hook=_noop_hook),
        "mcp.remove": lambda owner, payload: mcp_service.remove_mcp_server(owner, payload["name"], config_change_hook=_noop_hook),
    }
    await handlers._run_mcp_server_command(session, data, command=command, invoke=operations[command], reload_other_managers=True)
    receipt = session.events[-1]
    assert receipt["level"] == "warning" and receipt["data"]["configuration_saved"] is True
    assert receipt["data"]["runtime_applied"] is False
    assert receipt["data"]["runtime_errors"] == [{"phase": "reload", "message": "actual reload failure"}]
    saved = json.loads(path.read_text())["servers"]
    if command == "mcp.remove":
        assert "docs" not in saved
    elif command == "mcp.toggle":
        assert saved["docs"]["auto_start"] is False
    else:
        assert saved["docs"]["command"] == "node"


@pytest.mark.asyncio
async def test_actual_write_failure_never_claims_configuration_was_saved(tmp_path, monkeypatch):
    path = _actual_config(tmp_path, monkeypatch, {"servers": {}})
    before = path.read_bytes()
    manager = SimpleNamespace(workspace_root=tmp_path)
    session = _CommandSession(manager)

    def fail_write(*_args):
        raise OSError("actual write failure")

    monkeypatch.setattr(config_file, "atomic_write_text", fail_write)
    await handlers._run_mcp_server_command(session, {"name": "docs", "transport": "stdio", "command": "node"},
        command="mcp.add", invoke=lambda owner, payload: mcp_service.add_mcp_server(owner, payload, config_change_hook=_noop_hook))
    assert session.events[-1]["level"] == "error"
    assert session.events[-1]["data"]["configuration_saved"] is False
    assert path.read_bytes() == before


@pytest.mark.asyncio
async def test_cancellation_before_write_preserves_the_original_configuration(tmp_path, monkeypatch):
    path = _actual_config(tmp_path, monkeypatch, {"servers": {}})
    before = path.read_bytes()
    started = asyncio.Event()

    async def hook(**_kwargs):
        started.set()
        await asyncio.Event().wait()

    session = _CommandSession(SimpleNamespace(workspace_root=tmp_path))
    task = asyncio.create_task(handlers._run_mcp_server_command(session,
        {"name": "docs", "transport": "stdio", "command": "python"}, command="mcp.add",
        invoke=lambda owner, payload: mcp_service.add_mcp_server(owner, payload, config_change_hook=hook)))
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert path.read_bytes() == before and not session.events


@pytest.mark.asyncio
async def test_cancellation_after_write_commits_a_saved_runtime_warning(tmp_path, monkeypatch):
    path = _actual_config(tmp_path, monkeypatch, {"servers": {"docs": {"transport": "stdio", "command": "python"}}})
    started = asyncio.Event()

    class Manager:
        workspace_root = tmp_path

        async def reload_config(self):
            started.set()
            await asyncio.Event().wait()

        def get_all_status(self):
            return []

    session = _CommandSession(Manager())
    monkeypatch.setattr(_state, "bootstrap", None)
    task = asyncio.create_task(handlers._run_mcp_server_command(session, {"name": "docs"}, command="mcp.remove",
        invoke=lambda owner, payload: mcp_service.remove_mcp_server(owner, payload["name"], config_change_hook=_noop_hook)))
    await asyncio.wait_for(started.wait(), 1)
    assert "docs" not in json.loads(path.read_text())["servers"]
    task.cancel()
    assert await asyncio.wait_for(task, .3) is True
    receipt = session.events[-1]
    assert receipt["level"] == "warning" and receipt["data"]["configuration_saved"] is True
    assert receipt["data"]["runtime_applied"] is False and "cancelled" in receipt["message"]


@pytest.mark.timeout(45)
def test_cancel_after_persisted_remove_does_not_replay_the_mutation_on_reconnect(tmp_path, monkeypatch):
    _install_noop_llm(monkeypatch)
    path = _actual_config(tmp_path, monkeypatch, {"servers": {"docs": {"transport": "stdio", "command": "python"}}})
    monkeypatch.setattr("backend.main.CONVERSATION_DATA_DIR", tmp_path / "conversations", raising=False)
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", tmp_path / "conversations", raising=False)
    started = threading.Event()

    class Manager:
        workspace_root = None
        reloads = 0

        async def reload_config(self):
            self.reloads += 1
            started.set()
            await asyncio.Event().wait()

        def get_all_status(self):
            return []

    manager = Manager()
    with TestClient(app) as client:
        with client.websocket_connect("/ws?session_id=mcp-remove-cancel") as socket:
            _receive_until_event(socket, "llm.model.updated")
            session = _state.ws_manager.get_session("mcp-remove-cancel")
            session.mcp_manager = manager
            monkeypatch.setattr(session, "refresh_tool_registry_if_mcp_changed", lambda **_kwargs: True)
            socket.send_json({"type": "mcp.remove", "name": "docs", "client_command_id": "remove-config"})
            _receive_until_event(socket, "client.command.ack")
            assert started.wait(5)

            async def cancel_command():
                tasks = [task for task in session.command_dispatcher.command_tasks if not task.done()]
                assert len(tasks) == 1
                tasks[0].cancel()

            client.portal.call(cancel_command)
            while True:
                event = socket.receive_json()
                if event.get("type") == "command.result" and event.get("client_command_id") == "remove-config":
                    break
            assert event["level"] == "warning" and event["data"]["configuration_saved"] is True
        with client.websocket_connect("/ws?session_id=mcp-remove-cancel") as socket:
            _receive_until_event(socket, "llm.model.updated")
            socket.send_json({"type": "session.restore", "client_command_id": "restore-remove"})
            _receive_until_event(socket, "session.restored", max_attempts=100)
            assert manager.reloads == 1
            assert session.run_manager.durable_client_commands.is_client_command_completed("remove-config")
            assert "docs" not in json.loads(path.read_text())["servers"]


@pytest.mark.asyncio
async def test_rename_retirement_failure_reports_the_already_saved_new_identity(tmp_path, monkeypatch):
    path = _actual_config(tmp_path, monkeypatch, {"servers": {"docs": {"transport": "stdio", "command": "python"}}})

    class Manager:
        workspace_root = tmp_path

        async def remove_server(self, name):
            assert name == "docs"
            raise RuntimeError("old connection cleanup is pending")

        def get_all_status(self):
            return []

    session = _CommandSession(Manager())
    monkeypatch.setattr(_state, "bootstrap", None)
    await handlers._run_mcp_server_command(session,
        {"original_name": "docs", "name": "renamed", "transport": "stdio", "command": "python"},
        command="mcp.update", invoke=lambda owner, payload: mcp_service.update_mcp_server(owner, payload, config_change_hook=_noop_hook))
    receipt = session.events[-1]
    assert receipt["level"] == "warning" and receipt["data"]["configuration_saved"] is True
    assert receipt["data"]["runtime_errors"][0]["phase"] == "retire_previous_server"
    assert set(json.loads(path.read_text())["servers"]) == {"renamed"}


@pytest.mark.asyncio
@pytest.mark.parametrize("approved", [True, False])
async def test_project_decision_saved_before_reload_failure_keeps_its_real_owner(tmp_path, monkeypatch, approved):
    from backend.mcp import project_settings

    _actual_config(tmp_path, monkeypatch, {"servers": {}})
    monkeypatch.setattr(project_settings, "is_workspace_trusted", lambda _: True)

    class Manager:
        workspace_root = tmp_path

        def get_all_status(self):
            return [{"name": "docs", "source": "project", "project_workspace": str(tmp_path),
                     "status": "offline", "phase": "stopped"}]

        async def reload_config(self):
            raise OSError("project runtime reload failure")

    session = _CommandSession(Manager())
    session.session_id = "project-owner"
    command = handlers.handle_mcp_project_approve if approved else handlers.handle_mcp_project_reject
    with bind_execution_command_scope(session.session_id, CommandScope("owner", str(tmp_path), "project-request")):
        await command(session, {"conversation_id": "owner", "workspace_root": str(tmp_path), "name": "docs"})
    receipt = session.events[-1]
    assert receipt["level"] == "warning" and receipt["data"]["configuration_saved"] is True
    assert receipt["data"]["runtime_applied"] is False
    settings = json.loads(project_settings.project_local_settings_path(tmp_path).read_text())
    assert settings["enabled_servers" if approved else "disabled_servers"] == ["docs"]


def test_skill_dependency_saved_before_reload_failure_is_not_reported_as_uninstalled(tmp_path, monkeypatch):
    from backend.agent.skill_activation import activate_turn_skills
    from backend.agent.state import AgentState
    from backend.permissions.context import PermissionContext
    from backend.sandbox.policy import NetworkSandboxPolicy, ResolvedSandboxPolicy, SandboxEnforcement
    from backend.tests.test_skill_mcp_dependency_policy import _McpManager, _skill_manager

    skills = _skill_manager(tmp_path, monkeypatch)
    path = _actual_config(tmp_path, monkeypatch, {"servers": {}})

    class Manager(_McpManager):
        async def reload_config(self):
            raise OSError("Skill runtime reload failure")

    manager = Manager()
    real_install = mcp_service.install_skill_mcp_servers

    async def install(owner, candidates):
        return await real_install(owner, candidates, config_change_hook=_noop_hook)

    monkeypatch.setattr(mcp_service, "install_skill_mcp_servers", install)
    state = AgentState(user_message="$read-docs", workspace_root=tmp_path)

    async def activate():
        return [event async for event in activate_turn_skills(skills, state.user_message, state, manager,
            permission_context=PermissionContext(approval_policy="never"),
            permission_profile=ResolvedSandboxPolicy(SandboxEnforcement.DISABLED, NetworkSandboxPolicy.RESTRICTED),
            originator="Codex fixture")]

    events = asyncio.run(activate())
    assert "docs" in json.loads(path.read_text())["servers"]
    content = [event.data["content"] for event in events if event.type == "system_notice"]
    assert any("were saved, but runtime refresh is incomplete" in item for item in content)
    assert not any("Could not install MCP dependencies" in item for item in content)
    assert "MCP dependency unavailable: docs" in state.prompt_context["skill_injections"][0]["content"]


@pytest.mark.timeout(45)
def test_saved_config_warning_survives_actual_websocket_reconnection(tmp_path, monkeypatch):
    _install_noop_llm(monkeypatch)
    path = _actual_config(tmp_path, monkeypatch, {"servers": {}})
    monkeypatch.setattr("backend.main.CONVERSATION_DATA_DIR", tmp_path / "conversations", raising=False)
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", tmp_path / "conversations", raising=False)
    started, release = threading.Event(), threading.Event()

    class Manager:
        workspace_root = None
        reloads = 0

        async def reload_config(self):
            self.reloads += 1
            started.set()
            while not release.is_set():
                try:
                    await asyncio.sleep(.01)
                except asyncio.CancelledError:
                    continue
            raise OSError("reload failed after reconnect")

        def get_all_status(self):
            return []

    manager = Manager()
    with TestClient(app) as client:
        with client.websocket_connect("/ws?session_id=mcp-config-reconnect") as socket:
            _receive_until_event(socket, "llm.model.updated")
            session = _state.ws_manager.get_session("mcp-config-reconnect")
            session.mcp_manager = manager
            monkeypatch.setattr(session, "refresh_tool_registry_if_mcp_changed", lambda **_kwargs: True)
            socket.send_json({"type": "mcp.add", "name": "saved", "transport": "stdio", "command": "python",
                              "auto_start": False, "client_command_id": "saved-config"})
            _receive_until_event(socket, "client.command.ack")
            assert started.wait(5)
        with client.websocket_connect("/ws?session_id=mcp-config-reconnect") as socket:
            _receive_until_event(socket, "llm.model.updated")
            socket.send_json({"type": "session.restore", "client_command_id": "restore-config"})
            release.set()
            while True:
                event = socket.receive_json()
                if event.get("type") == "command.result" and event.get("client_command_id") == "saved-config":
                    break
            assert event["level"] == "warning"
            assert event["data"]["configuration_saved"] is True and event["data"]["runtime_applied"] is False
            assert "reload failed after reconnect" in event["message"]
            assert manager.reloads == 1
            assert "saved" in json.loads(path.read_text())["servers"]


def _bootstrap():
    async def status(*_args):
        pass

    return AppBootstrap(build_tool_registry=lambda *_args, **_kwargs: ToolRegistry(),
        create_session_llm=lambda **_kwargs: None, ws_manager=None, on_mcp_status_change=status)


@pytest.mark.asyncio
async def test_workspace_reload_settles_every_manager_and_preserves_error_owner():
    bootstrap = _bootstrap()
    entered, release = asyncio.Event(), asyncio.Event()

    class Failed:
        async def reload_config(self):
            raise OSError("A rejected config")

    class Slow:
        async def reload_config(self):
            entered.set()
            await release.wait()

    bootstrap._mcp_managers = {"workspace-A": Failed(), "workspace-B": Slow()}
    reload = asyncio.create_task(bootstrap.reload_mcp_managers())
    await asyncio.wait_for(entered.wait(), 1)
    assert not reload.done()
    release.set()
    with pytest.raises(MCPManagerReloadError) as failure:
        await reload
    assert failure.value.failures == [{"workspace_root": "workspace-A", "error_type": "OSError", "message": "A rejected config"}]
    await asyncio.sleep(0)
    assert not bootstrap._mcp_reload_tasks and not bootstrap._mcp_reload_owners


@pytest.mark.asyncio
async def test_partial_workspace_publication_has_an_owner_specific_warning(tmp_path, monkeypatch):
    bootstrap = _bootstrap()
    path = _actual_config(tmp_path, monkeypatch, {"servers": {}})
    release, started = asyncio.Event(), asyncio.Event()

    class Primary:
        workspace_root = tmp_path

        async def reload_config(self):
            pass

        def get_all_status(self):
            return []

    class Failed:
        async def reload_config(self):
            raise OSError("other workspace rejected config")

    class Slow:
        async def reload_config(self):
            started.set()
            await release.wait()

    primary = Primary()
    session = _CommandSession(primary)
    bootstrap._mcp_managers = {"primary": primary, "failed-workspace": Failed(), "slow-workspace": Slow()}
    monkeypatch.setattr(_state, "bootstrap", bootstrap)
    request = asyncio.create_task(handlers._run_mcp_server_command(session,
        {"name": "docs", "transport": "stdio", "command": "python"}, command="mcp.add",
        invoke=lambda owner, payload: mcp_service.add_mcp_server(owner, payload, config_change_hook=_noop_hook),
        reload_other_managers=True))
    await asyncio.wait_for(started.wait(), 1)
    assert not request.done() and not session.events
    release.set()
    await request
    result = session.events[-1]
    assert result["level"] == "warning" and result["data"]["configuration_saved"] is True
    assert result["data"]["runtime_errors"] == [{"phase": "reload_other_workspaces", "workspace_root": "failed-workspace",
        "error_type": "OSError", "message": "other workspace rejected config"}]
    assert "docs" in json.loads(path.read_text())["servers"]


@pytest.mark.asyncio
async def test_cancelled_reload_stays_owned_and_cannot_launch_after_shutdown(tmp_path, monkeypatch, caplog):
    bootstrap = _bootstrap()
    manager = MCPServerManager(workspace_root=tmp_path)
    bootstrap._mcp_managers["workspace"] = manager
    entered, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def reload():
        entered.set()
        await _resist_cancellation(release, cancelled)
        await manager.start_server(MCPServerConfig(name="late", command="python"))

    monkeypatch.setattr(manager, "_reload_config_unlocked", reload)
    monkeypatch.setattr("backend.bootstrap.app.CANCELLATION_DRAIN_TIMEOUT_SECONDS", .02)
    request = asyncio.create_task(bootstrap.reload_mcp_managers())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(request, .3)
        assert bootstrap._mcp_reload_tasks
        await asyncio.wait_for(bootstrap.shutdown(), 2)
        assert bootstrap._mcp_reload_tasks and bootstrap._mcp_managers["workspace"] is manager
        assert "configuration reload" in caplog.text
        with pytest.raises(RuntimeError, match="shutdown"):
            await bootstrap.ensure_mcp_manager(tmp_path / "another")
    finally:
        pending = tuple(bootstrap._mcp_reload_tasks)
        release.set()
        outcomes = await asyncio.gather(*pending, return_exceptions=True)
        assert outcomes and isinstance(outcomes[0], RuntimeError)
        assert not manager._servers
        await bootstrap.shutdown()
