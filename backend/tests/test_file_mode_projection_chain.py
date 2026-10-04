from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.agent.message import AgentEvent
from backend.terminal.session import TerminalSession
from backend.terminal.session import TerminalSessionManager
from backend.ws.handlers import diff
from backend.ws.handlers.terminal import handle_terminal_mirror_created, handle_terminal_mirror_output
from backend.ws.session_lifecycle import SessionLifecycle
from backend.ws.event_log import sanitize_ws_live_payload, sanitize_ws_replay_payload


@pytest.mark.asyncio
async def test_pipe_cursors_match_renderer_offsets_across_snapshot_trim_and_clear():
    chunks = iter(["tick\r\n", "tick\r\n", "😀\r\n", ""])
    events = []
    snapshots = []
    session = TerminalSession("pipe", conversation_id="owner")
    session._MAX_OUTPUT_BUFFER_CHARS = 8

    async def read(_size):
        return next(chunks).encode("utf-8")

    async def capture(session_id, data, start, end):
        events.append((session_id, data, start, end))
        snapshots.append(session.snapshot(max_chars=8))

    session._on_output = capture
    session._process = SimpleNamespace(stdout=SimpleNamespace(read=read), returncode=None, pid=10)
    await session._read_stdout()
    assert events == [("pipe", "tick\r\n", 0, 6), ("pipe", "tick\r\n", 6, 12), ("pipe", "😀\r\n", 12, 16)]
    assert snapshots[0]["output_start_cursor"] == 0
    assert snapshots[0]["output_end_cursor"] == 6
    assert snapshots[-1]["output"] == "ick\r\n😀\r\n"
    assert snapshots[-1]["output_start_cursor"] == 7
    assert snapshots[-1]["output_end_cursor"] == 16
    session.clear_output()
    cleared = session.snapshot()
    assert cleared["output"] == ""
    assert cleared["output_start_cursor"] == cleared["output_end_cursor"] == 16
    session.append_external_output("next")
    assert session.snapshot()["output_start_cursor"] == 16
    assert session.snapshot()["output_end_cursor"] == 20


@pytest.mark.asyncio
async def test_terminal_output_envelope_keeps_cursor_and_original_owner():
    owner = SimpleNamespace(send_payload=AsyncMock())
    lifecycle = object.__new__(SessionLifecycle)
    lifecycle._session = owner
    await lifecycle.on_terminal_output("pipe", "😀", 40, 42, "original-owner")
    payload = owner.send_payload.await_args.args[0]
    assert payload == {
        "type": "terminal.output", "session_id": "pipe", "data": "😀",
        "start_cursor": 40, "end_cursor": 42, "conversation_id": "original-owner",
    }


def test_terminal_redaction_and_replay_preserve_utf16_cursor_spans():
    token = "sk-" + "x" * 36
    content = f"😀 {token}\r\n" + "line\r\n" * 4_000
    end = len(content.encode("utf-16-le")) // 2
    payload = {
        "type": "terminal.snapshot", "session_id": "pipe", "conversation_id": "owner",
        "output": content, "output_start_cursor": 0, "output_end_cursor": end,
        "output_chars": end, "total_output_chars": end,
    }
    live = sanitize_ws_live_payload(payload)
    assert token not in live["output"]
    assert len(live["output"].encode("utf-16-le")) // 2 == end
    replay = sanitize_ws_replay_payload(live)
    assert replay["output"] == live["output"][-16_000:]
    assert replay["output_end_cursor"] - replay["output_start_cursor"] == len(replay["output"].encode("utf-16-le")) // 2
    assert replay["truncated"] is True
    chunk = {"type": "terminal.output", "data": token, "start_cursor": 100, "end_cursor": 100 + len(token)}
    assert sanitize_ws_live_payload(chunk)["data"] == "*" * len(token)
    non_bmp_secret = "password=" + "😀" * 8
    secret_units = len(non_bmp_secret.encode("utf-16-le")) // 2
    masked = sanitize_ws_live_payload({**chunk, "data": non_bmp_secret, "end_cursor": 100 + secret_units})
    assert masked["data"] == "*" * secret_units


def git_session(tmp_path):
    events = []

    async def send_event(event):
        events.append(event)

    async def result(command, message, **kwargs):
        events.append(AgentEvent.command_result(command, message, **kwargs))

    return SimpleNamespace(
        active_conversation_id="owner", conversation_repo=None,
        session_lifecycle=SimpleNamespace(workspace_root=tmp_path, current_workspace_root=lambda: tmp_path),
        resolve_requested_workspace=lambda _root: tmp_path,
        validate_git_relative_path=lambda value: value,
        send_event=send_event, emit_command_result=result, events=events,
    )


@pytest.mark.asyncio
async def test_git_action_settles_waiting_caller_after_scoped_diff_event(tmp_path, monkeypatch):
    monkeypatch.setattr("backend.diff.git_integration.stage_file", AsyncMock(return_value=True))
    session = git_session(tmp_path)
    await diff.handle_diff_git_stage_file(session, {
        "path": "file.txt", "conversation_id": "owner", "client_command_id": "stage-one",
    })
    assert [event.type for event in session.events] == ["diff.git_stage_file", "command.result"]
    event, result = session.events
    assert event.data["request_id"] == "stage-one"
    assert result.data["command"] == "diff.git_stage_file"
    assert result.data["level"] == "success"
    assert result.data["data"] == {
        "ok": True, "conversation_id": "owner", "workspace_root": str(tmp_path), "request_id": "stage-one",
    }


@pytest.mark.asyncio
async def test_git_action_failure_is_a_single_failed_completion(tmp_path, monkeypatch):
    monkeypatch.setattr("backend.diff.git_integration.stage_file", AsyncMock(side_effect=RuntimeError("index.lock exists")))
    session = git_session(tmp_path)
    await diff.handle_diff_git_stage_file(session, {"path": "file.txt", "conversation_id": "owner"})
    assert len(session.events) == 1
    assert session.events[0].type == "command.result"
    assert session.events[0].data["level"] == "error"
    assert "index.lock exists" in session.events[0].data["message"]


@pytest.mark.asyncio
async def test_native_mirror_snapshot_and_overlap_reconstruct_one_output_stream(tmp_path):
    session = git_session(tmp_path)
    session.terminal_manager = TerminalSessionManager()
    session.conversation_repo = SimpleNamespace(get_conversation=lambda _id: object())
    created = {
        "session_id": "native", "conversation_id": "owner", "pid": 10,
        "output": "tick\r\n", "output_start_cursor": 0, "output_end_cursor": 6,
    }
    await handle_terminal_mirror_created(session, created)
    await handle_terminal_mirror_output(session, {
        "session_id": "native", "conversation_id": "owner", "data": "tick\r\n",
        "start_cursor": 6, "end_cursor": 12,
    })
    await handle_terminal_mirror_output(session, {
        "session_id": "native", "conversation_id": "owner", "data": "tick\r\n",
        "start_cursor": 6, "end_cursor": 12,
    })
    assert session.terminal_manager.snapshot("native", conversation_id="owner")["output"] == "tick\r\ntick\r\n"
    session.terminal_manager = TerminalSessionManager()
    await handle_terminal_mirror_created(session, {**created, "output": "tick\r\ntick\r\n", "output_end_cursor": 12})
    assert session.terminal_manager.snapshot("native", conversation_id="owner")["output"] == "tick\r\ntick\r\n"


@pytest.mark.asyncio
async def test_native_mirror_rejects_malformed_cursor_before_registration(tmp_path):
    session = git_session(tmp_path)
    session.terminal_manager = TerminalSessionManager()
    session.conversation_repo = SimpleNamespace(get_conversation=lambda _id: object())
    await handle_terminal_mirror_created(session, {
        "session_id": "native", "conversation_id": "owner", "pid": 10,
        "output": "😀", "output_start_cursor": 0, "output_end_cursor": 1,
    })
    assert session.terminal_manager.get_session("native") is None
    assert session.events[0].data["level"] == "error"
