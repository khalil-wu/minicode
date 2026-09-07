from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.config import PermissionSettings
from backend.permissions.checker import PermissionChecker
from backend.services.workspace_service import resolve_requested_workspace
from backend.ws.command_scope import resolve_command_scope
from backend.ws.handlers import preview, terminal


def _session(workspace: Path | None, bound_workspace: str = "") -> SimpleNamespace:
    conversation = SimpleNamespace(id="workspace-scope-owner", workspace_root=bound_workspace, worktree_path="")
    checker = PermissionChecker(PermissionSettings())
    return SimpleNamespace(
        session_id="workspace-scope-session",
        active_conversation_id=conversation.id,
        conversation_repo=SimpleNamespace(get_conversation=lambda owner: conversation),
        session_lifecycle=SimpleNamespace(workspace_root=workspace, current_workspace_root=lambda: workspace),
        resolve_requested_workspace=lambda requested: resolve_requested_workspace(workspace, requested),
        permission_checker=checker,
        permission_context_for_conversation=lambda conversation, source: checker.build_context(source=source),
        tool_registry=SimpleNamespace(get_tool=lambda name: object()),
        artifact_store=None,
        background_manager=None,
        terminal_manager=SimpleNamespace(create_session=AsyncMock()),
        approval_handler=None,
        send_event=AsyncMock(),
        send_payload=AsyncMock(),
    )


@pytest.mark.parametrize("bound", [False, True], ids=["never-bound", "failed-restore"])
@pytest.mark.parametrize("command,handler", [
    ("terminal.create", terminal.handle_terminal_create),
    ("terminal.exec", terminal.handle_terminal_exec),
    ("preview.launch.config", preview.handle_preview_launch_config),
    ("preview.launch.start", preview.handle_preview_launch_start),
])
def test_workspace_commands_reject_missing_mount_before_accessing_the_server_cwd(
    tmp_path: Path, monkeypatch, bound: bool, command: str, handler,
) -> None:
    server = tmp_path / "server"
    server.mkdir()
    (server / "package.json").write_text(json.dumps({"scripts": {"dev": "server-owned-command"}}))
    monkeypatch.chdir(server)
    session = _session(None, str(tmp_path) if bound else "")
    execute = AsyncMock()
    start = AsyncMock()
    monkeypatch.setattr("backend.services.terminal_service.run_terminal_exec_command", execute)
    monkeypatch.setattr("backend.preview.start_preview_launch", start)

    asyncio.run(handler(session, {
        "conversation_id": session.active_conversation_id,
        "command": "pwd",
        "cwd": str(server),
    }))

    session.send_event.assert_awaited_once()
    event = session.send_event.await_args.args[0]
    assert event.type == "command.result"
    assert event.data["command"] == command
    assert event.data["level"] == "error"
    assert "Open a workspace" in event.data["message"]
    session.send_payload.assert_not_awaited()
    session.terminal_manager.create_session.assert_not_awaited()
    execute.assert_not_awaited()
    start.assert_not_awaited()


def test_workspace_is_optional_for_inventory_but_cannot_be_supplied_as_an_unbound_authority(tmp_path: Path) -> None:
    session = _session(None)

    assert resolve_command_scope(session, {}).workspace_root == ""
    with pytest.raises(ValueError, match="Open a workspace"):
        resolve_command_scope(session, {}, require_workspace=True)
    with pytest.raises(ValueError, match="Open a workspace"):
        resolve_command_scope(session, {"workspace_root": str(tmp_path)}, require_workspace=True)


def test_mounted_workspace_reaches_terminal_execution_and_preview_config(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "selected"
    nested = workspace / "nested"
    nested.mkdir(parents=True)
    (workspace / "package.json").write_text(json.dumps({"scripts": {"dev": "vite"}}))
    session = _session(workspace, str(workspace))
    execute = AsyncMock(return_value={"type": "terminal.output", "output": "fixture", "exit_code": 0})
    monkeypatch.setattr("backend.services.terminal_service.run_terminal_exec_command", execute)

    async def scenario() -> None:
        await terminal.handle_terminal_exec(session, {"command": "pwd", "cwd": str(nested)})
        await preview.handle_preview_launch_config(session, {})

    asyncio.run(scenario())

    execute.assert_awaited_once()
    assert execute.await_args.args == ("pwd", str(nested))
    assert execute.await_args.kwargs["context"].workspace_root == workspace
    assert execute.await_args.kwargs["context"].conversation_id == session.active_conversation_id
    event = session.send_event.await_args.args[0]
    assert event.type == "preview.launch.config"
    assert event.data["workspace_root"] == str(workspace)
    assert event.data["configs"][0]["cwd"] == str(workspace)
