from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.preview import launcher
from backend.services.workspace_service import resolve_requested_workspace
from backend.ws.handlers.preview import handle_preview_launch_stop


async def _monitor_output(payload: bytes, stream_name: str, workspace: Path):
    streams = {name: asyncio.StreamReader() for name in ("stdout", "stderr")}
    streams[stream_name].feed_data(payload)
    for stream in streams.values():
        stream.feed_eof()
    launched = launcher.PreviewLaunchProcess(
        id="output-fixture",
        config=launcher.PreviewLaunchConfig("web", "unused", str(workspace)),
        process=SimpleNamespace(pid=1234, returncode=None, **streams),
        session_id="session-fixture",
        conversation_id="conversation-fixture",
        workspace_root=str(workspace),
        _sandbox_runner=SimpleNamespace(terminate=AsyncMock(return_value=True)),
    )

    async def wait():
        launched.process.returncode = 0
        return 0

    launched.process.wait = wait
    events = []

    async def broadcast(event):
        events.append(event)
        if event["type"] == "preview.server.ready":
            launched.process.returncode = 0
            launched._exit_event.set()

    await launcher._monitor_process(launched, broadcast)
    return launched, events


@pytest.mark.parametrize("stream_name", ["stdout", "stderr"])
@pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["lf", "crlf"])
def test_preview_monitor_drains_long_lines_without_losing_the_ready_url(
    tmp_path: Path, stream_name: str, newline: str,
) -> None:
    long_line = "x" * (96 * 1024) + " LONG_LOG_END"
    payload = f"{long_line}{newline}Local: http://127.0.0.1:43127/{newline}".encode()

    launched, events = asyncio.run(_monitor_output(payload, stream_name, tmp_path))

    output = [event for event in events if event["type"] == "preview.server.output"]
    assert len(output) == 2
    assert output[0]["stream"] == stream_name
    assert "characters omitted" in output[0]["line"]
    assert output[0]["line"].endswith(" LONG_LOG_END")
    assert len(output[0]["line"]) < launcher.MAX_PREVIEW_LOG_LINE_CHARS + 80
    assert output[1]["line"] == "Local: http://127.0.0.1:43127/"
    assert launched.detected_url == "http://127.0.0.1:43127/"
    assert launched.ready_event.is_set()
    assert launched.status == "exited"
    if stream_name == "stderr":
        assert list(launched.stderr_tail) == [event["line"] for event in output]


@pytest.mark.parametrize("final_newline", [False, True], ids=["eof", "newline"])
def test_preview_monitor_decodes_split_utf8_and_flushes_the_final_line(tmp_path: Path, final_newline: bool) -> None:
    first_line = "x" * 4095 + "中文😀"
    ready_line = "Local: http://127.0.0.1:43128/"
    payload = f"{first_line}\r\n{ready_line}" + ("\n" if final_newline else "")

    launched, events = asyncio.run(_monitor_output(payload.encode("utf-8"), "stdout", tmp_path))

    output_lines = [event["line"] for event in events if event["type"] == "preview.server.output"]
    assert output_lines == [first_line, ready_line]
    assert launched.detected_url == "http://127.0.0.1:43128/"


def test_preview_stop_handler_only_stops_the_current_workspace(monkeypatch, tmp_path: Path) -> None:
    previous = tmp_path / "previous"
    current = tmp_path / "current"
    previous.mkdir()
    current.mkdir()
    processes = {
        name: launcher.PreviewLaunchProcess(
            id=name,
            config=launcher.PreviewLaunchConfig("web", "unused", str(workspace)),
            process=SimpleNamespace(pid=1234, returncode=None),
            session_id="shared-session",
            conversation_id="same-conversation",
            workspace_root=str(workspace),
        )
        for name, workspace in (("previous", previous), ("current", current))
    }
    monkeypatch.setattr(launcher, "_RUNNING", dict(processes))

    async def terminate(process):
        process.returncode = 0
        return True

    monkeypatch.setattr(launcher, "terminate_process_tree", terminate)
    conversation = SimpleNamespace(id="same-conversation", workspace_root=str(current), worktree_path="")
    send_event = AsyncMock()
    session = SimpleNamespace(
        session_id="shared-session",
        active_conversation_id=conversation.id,
        conversation_repo=SimpleNamespace(get_conversation=lambda owner: conversation),
        session_lifecycle=SimpleNamespace(workspace_root=current, current_workspace_root=lambda: current),
        resolve_requested_workspace=lambda requested: resolve_requested_workspace(current, requested),
        send_event=send_event,
    )

    asyncio.run(handle_preview_launch_stop(session, {
        "conversation_id": conversation.id, "workspace_root": str(current),
    }))

    assert processes["previous"].process.returncode is None
    assert processes["current"].process.returncode == 0
    assert launcher._RUNNING == {"previous": processes["previous"]}
    send_event.assert_awaited_once()
    assert send_event.await_args.args[0].data["id"] == "current"


@pytest.mark.parametrize("cwd", ["..", "nested"], ids=["outside", "inside"])
def test_preview_launch_cwd_is_not_silently_replaced(tmp_path: Path, cwd: str) -> None:
    (tmp_path / ".minicode").mkdir()
    (tmp_path / "nested").mkdir()
    (tmp_path / ".minicode" / "launch.json").write_text(json.dumps({"configurations": [{
        "name": "web", "command": "unused", "cwd": cwd,
    }]}), encoding="utf-8")

    if cwd == "..":
        with pytest.raises(launcher.PreviewLaunchConfigError, match="cwd must stay inside"):
            launcher.load_preview_launch_configs(tmp_path)
    else:
        config = launcher.load_preview_launch_configs(tmp_path)[0]
        assert config.cwd == str(tmp_path / "nested")
