from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from backend.agent.message import UserCommand
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.tests.test_ws_cold_connection import _connection, _release_session
from backend.tools.read_file import ReadFileTool
from backend.workspace.context import WorkspaceContext
from backend.ws.manager import WebSocketManager


@pytest.mark.asyncio
async def test_two_switches_to_the_same_target_publish_only_the_latest_receipt(tmp_path, monkeypatch):
    manager = WebSocketManager()
    connection = _connection(tmp_path, session_id="same-target-navigation")
    session, _ = await manager.connect(**connection)
    monkeypatch.setattr(session, "refresh_llm_selection", lambda **_: None)
    monkeypatch.setattr(session.session_lifecycle, "schedule_runtime_capabilities", lambda **_: None)
    original = session.conversation_repo.create_conversation(title="Original")
    target = session.conversation_repo.create_conversation(title="Target")
    session.active_conversation_id = original.id
    entered, release = threading.Event(), threading.Event()
    get_view = session.conversation_repo.get_conversation_view
    held_once = False
    def held_view(owner):
        nonlocal held_once
        if owner == target.id and not held_once:
            held_once = True
            entered.set()
            assert release.wait(3)
        return get_view(owner)
    monkeypatch.setattr(session.conversation_repo, "get_conversation_view", held_view)
    first = asyncio.create_task(session.command_dispatcher._dispatch_client_command(UserCommand("conversation.switch",{
        "conversation_id":target.id,"client_command_id":"first-target",
    }),session.connection_generation))
    second = None
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        second = asyncio.create_task(session.command_dispatcher._dispatch_client_command(UserCommand("conversation.switch",{
            "conversation_id":target.id,"client_command_id":"latest-target",
        }),session.connection_generation))
        await asyncio.sleep(0)
        release.set()
        assert all(await asyncio.gather(first, second))
        await session.conversation_runtime.wait_for_hydration(target.id)
        await session.event_outbox.drain_delivery()
        assert session.active_conversation_id == target.id
        pages = [event for event in connection["websocket"].sent if event.get("type")=="conversation.switched"]
        assert pages and all(event.get("client_command_id")=="latest-target" for event in pages)
        superseded = next(event for event in connection["websocket"].sent if event.get("type")=="command.result" and event.get("client_command_id")=="first-target")
        assert superseded["data"]["superseded"] is True
    finally:
        release.set()
        await asyncio.gather(first,*([second] if second is not None else []),return_exceptions=True)
        await _release_session(session)


@pytest.mark.asyncio
@pytest.mark.parametrize("first_kind,same_target", [("conversation.create", False), ("conversation.create", True), ("workspace.set", False)])
async def test_superseded_creation_finishes_without_publishing_a_workspace_or_stealing_selection(tmp_path, monkeypatch, first_kind, same_target):
    initial, slow_root, selected_root = (tmp_path / name for name in ("initial", "slow", "selected"))
    for root in (initial, slow_root, selected_root):
        root.mkdir()
    monkeypatch.setattr("backend.workspace.trust.is_workspace_trusted", lambda _path: True)
    monkeypatch.setattr("backend.api.routes_health.get_mcp_manager", lambda: None)
    from backend.api import _state
    monkeypatch.setattr(_state, "bootstrap", None)
    entered, release = asyncio.Event(), asyncio.Event()
    initialize = WorkspaceContext.initialize
    held_once = False
    async def delayed_initialize(context):
        nonlocal held_once
        if context.root_path == slow_root and not held_once:
            held_once = True
            entered.set()
            await release.wait()
        return await initialize(context)
    monkeypatch.setattr(WorkspaceContext, "initialize", delayed_initialize)
    manager = WebSocketManager()
    connection = _connection(tmp_path, session_id="navigation-chain")
    session, _ = await manager.connect(**connection)
    monkeypatch.setattr(session, "refresh_llm_selection", lambda **_: None)
    monkeypatch.setattr(session.session_lifecycle, "schedule_runtime_capabilities", lambda **_: None)
    monkeypatch.setattr(session, "_run_cwd_changed_hook", AsyncMock())
    watcher = Mock()
    monkeypatch.setattr(session.session_lifecycle, "restart_file_watcher", watcher)
    original = session.conversation_repo.create_conversation(workspace_root=str(initial), title="Original")
    selected = session.conversation_repo.create_conversation(workspace_root=str(selected_root), title="Selected")
    session.active_conversation_id = original.id
    session.session_lifecycle.workspace_context = WorkspaceContext(initial)
    session.session_lifecycle.workspace_root = initial
    first_data = {"client_command_id":"slow-create", "permission_mode":"confirm"}
    if first_kind == "conversation.create":
        first_data.update(conversation_id="conv_created_slow", workspace_root=str(slow_root), title="Created slow", conversation_type="main")
    else:
        first_data["path"] = str(slow_root)
    first = asyncio.create_task(session.command_dispatcher._dispatch_client_command(UserCommand(first_kind, first_data), session.connection_generation))
    second = None
    try:
        await asyncio.wait_for(entered.wait(), 3)
        assert session.active_conversation_id == original.id
        assert session.session_lifecycle.workspace_root == initial
        target_id = "conv_created_slow" if same_target else selected.id
        second = asyncio.create_task(session.command_dispatcher._dispatch_client_command(UserCommand("conversation.switch", {
            "client_command_id":"latest-selection", "conversation_id":target_id,
        }), session.connection_generation))
        await asyncio.sleep(0)
        release.set()
        assert all(await asyncio.gather(first, second))
        await session.conversation_runtime.wait_for_hydration(target_id)
        await session.event_outbox.drain_delivery()
        records = session.conversation_repo.list_conversations()
        created = next(record for record in records if record.workspace_root == str(slow_root))
        assert session.conversation_repo.get_conversation(created.id) is not None
        assert session.active_conversation_id == target_id
        expected_root = slow_root if same_target else selected_root
        assert session.session_lifecycle.workspace_root == expected_root
        assert Path(session.session_lifecycle.workspace_context.root_path) == expected_root
        pages = [event for event in connection["websocket"].sent if event.get("type") == "conversation.switched"]
        assert pages and all(event.get("client_command_id") != "slow-create" for event in pages)
        assert pages[-1]["conversation_id"] == target_id
        assert pages[-1]["client_command_id"] == "latest-selection"
        outcome = next(event for event in connection["websocket"].sent if event.get("type") == "command.result" and event.get("client_command_id") == "slow-create")
        assert outcome["data"]["created"] is True
        assert outcome["data"]["activated"] is False
        assert slow_root not in [call.args[0] for call in watcher.call_args_list] or same_target
    finally:
        release.set()
        await asyncio.gather(first, *([second] if second is not None else []), return_exceptions=True)
        await _release_session(session)


@pytest.mark.asyncio
async def test_background_receipts_do_not_replace_foreground_selection_and_owned_tools_keep_their_workspace(tmp_path, monkeypatch):
    manager = WebSocketManager()
    connection = _connection(tmp_path, session_id="navigation-owned-tool")
    session, _ = await manager.connect(**connection)
    lifecycle = session.session_lifecycle
    session.active_conversation_id = "original"
    selected = lifecycle.admit_foreground_navigation(UserCommand("conversation.switch", {"conversation_id":"selected", "client_command_id":"selected-command"}))
    try:
        assert lifecycle.admit_foreground_navigation(UserCommand("conversation.create", {"activate":False,"client_command_id":"background-create"})) is None
        assert lifecycle.admit_foreground_navigation(UserCommand("conversation.delete", {"conversation_id":"original","client_command_id":"background-delete"})) is None
        assert lifecycle.admit_foreground_navigation(UserCommand("user_message", {"conversation_id":"original","client_command_id":"owned-message"})) is None
        with lifecycle.bind_foreground_navigation(selected):
            assert lifecycle.foreground_navigation_is_current()
        file = tmp_path / "owned.txt"
        file.write_text("Owned task still reads its real workspace", encoding="utf-8")
        tool_context = ToolExecutionContext(permission=PermissionContext(mode="bypass",workspace_root=tmp_path),workspace_root=tmp_path,conversation_id="original")
        with lifecycle.bind_foreground_navigation(None):
            result = await ReadFileTool(session.artifact_store).execute({"file_path":str(file)}, tool_context)
            assert not result.is_error
            assert "Owned task still reads" in result.content
    finally:
        await _release_session(session)
