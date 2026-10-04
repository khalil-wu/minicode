"""Controlled producer/restore checks; no shell, PTY, or OS process is started."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.services.terminal_service import terminal_list_payload, terminal_snapshot_payload
from backend.terminal.session import TerminalSession, TerminalSessionManager
from backend.ws.agent_runner import _project_subagent_done
from backend.ws.handlers.terminal import handle_terminal_mirror_created, handle_terminal_mirror_exit


def test_subagent_terminal_ui_snapshot_retains_cleanup_metadata():
    state = {"subagents": []}
    _project_subagent_done(state, {"subagent_id": "child", "status": "completed", "record": {
        "cleanup_pending": True, "cleanup_reason": "runner alive",
    }, "result": {"content": "retained"}})
    assert state["subagents"][0]["status"] == "done"
    assert state["subagents"][0]["cleanupPending"] is True
    assert state["subagents"][0]["cleanupReason"] == "runner alive"
    _project_subagent_done(state, {"subagent_id": "child", "status": "completed",
                                  "cleanup_pending": False, "cleanup_reason": ""})
    assert state["subagents"][0]["cleanupPending"] is False


def mirror_owner():
    return SimpleNamespace(terminal_manager=TerminalSessionManager(), active_conversation_id="owner",
                           active_terminal_session_id=None,
                           conversation_repo=SimpleNamespace(get_conversation=lambda owner: object()),
                           emit_command_result=AsyncMock(), send_payload=AsyncMock())


@pytest.mark.asyncio
async def test_known_exit_survives_mirror_restore_late_output_and_list():
    owner = mirror_owner()
    await handle_terminal_mirror_created(owner, {"session_id": "term", "conversation_id": "owner", "pid": 7,
        "is_alive": False, "exit_code": 9, "exit_signal": None, "exited_at": 1700,
        "output": "result", "output_start_cursor": 0, "output_end_cursor": 6})
    owner.terminal_manager.append_external_output("term", " tail", conversation_id="owner")
    snapshot = owner.terminal_manager.snapshot("term", conversation_id="owner")
    assert snapshot["exit_code"] == 9
    assert snapshot["exited_at"] == 1700
    assert snapshot["is_alive"] is False
    assert terminal_snapshot_payload(snapshot)["exit_code"] == 9
    assert terminal_list_payload(owner.terminal_manager.list_sessions(conversation_id="owner"), conversation_id="owner")["sessions"][0]["exit_code"] == 9


@pytest.mark.asyncio
async def test_signal_exit_stays_unknown_through_backend_mirror():
    owner = mirror_owner()
    owner.terminal_manager.upsert_external_session("term", pid=7, conversation_id="owner")
    await handle_terminal_mirror_exit(owner, {"session_id": "term", "conversation_id": "owner",
                                             "exit_code": None, "exit_signal": "SIGTERM", "exited_at": 2300})
    snapshot = owner.terminal_manager.snapshot("term", conversation_id="owner")
    assert snapshot["is_alive"] is False
    assert snapshot["exit_code"] is None
    assert snapshot["exit_signal"] == "SIGTERM"
    assert snapshot["exited_at"] == 2300


@pytest.mark.asyncio
async def test_pipe_snapshot_keeps_real_returncode_and_cleanup_projection():
    terminal = TerminalSession("term", conversation_id="owner")
    terminal._process = SimpleNamespace(returncode=-15, pid=7)
    await terminal._notify_exit_once()
    terminal.cleanup_pending = True
    terminal.cleanup_reason = "tree still alive"
    snapshot = terminal.snapshot()
    assert snapshot["exit_code"] == -15
    assert snapshot["exited_at"] > 0
    assert snapshot["cleanup_pending"] is True
    assert terminal.info.cleanup_reason == "tree still alive"
