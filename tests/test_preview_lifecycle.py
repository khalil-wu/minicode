from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.preview import launcher
from backend.preview.verifier import PreviewVerification
from backend.services.workspace_service import resolve_requested_workspace
from backend.subprocesses import spawn_exec
from backend.tools.preview_tool import PreviewServerTool
from backend.ws.handlers.preview import handle_preview_launch_start


def _launch_fixture(workspace: Path) -> launcher.PreviewLaunchProcess:
    return launcher.PreviewLaunchProcess(
        id="preview-lifecycle",
        config=launcher.PreviewLaunchConfig(
            "web", "fixture", str(workspace), port=43129, url="http://127.0.0.1:43129",
        ),
        process=SimpleNamespace(pid=1234, returncode=None),
        session_id="session-lifecycle",
        conversation_id="conversation-lifecycle",
        workspace_root=str(workspace),
    )


def test_preview_output_failure_terminates_the_process_and_reaps_both_readers(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(launcher, "_RUNNING", {})

    async def scenario():
        exit_event = asyncio.Event()
        child = await spawn_exec(
            sys.executable, "-u", "-c",
            "import sys; print('blocked', file=sys.stderr, flush=True); "
            "print('failure', flush=True); sys.stdin.readline()",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            on_exit=exit_event.set,
        )
        launched = _launch_fixture(tmp_path)
        launched.process = child
        launched._exit_event = exit_event
        launcher._RUNNING[launched.id] = launched
        reader_entered = asyncio.Event()
        reader_released = asyncio.Event()
        reader_cancelled = asyncio.Event()
        events = []

        async def broadcast(event):
            events.append(event)
            if event["type"] != "preview.server.output":
                return
            if event["stream"] == "stderr":
                reader_entered.set()
                try:
                    await reader_released.wait()
                finally:
                    reader_cancelled.set()
            else:
                await reader_entered.wait()
                raise RuntimeError("fixture broadcast failure")

        monitor = asyncio.create_task(launcher._monitor_process(launched, broadcast))
        launched._monitor_task = monitor
        try:
            await asyncio.wait_for(monitor, timeout=5)

            assert child.returncode is not None
            assert monitor.done()
            assert reader_cancelled.is_set()
            assert launched.id not in launcher._RUNNING
            assert launched.status == "crashed"
            assert events[-1]["type"] == "preview.server.crashed"
            assert "fixture broadcast failure" in events[-1]["stderr_tail"][-1]
            assert not [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
        finally:
            reader_released.set()
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)
            await launcher._stop_preview_processes([launched])

    asyncio.run(scenario())


@pytest.mark.parametrize("exit_code", [0, 7], ids=["normal", "crash"])
def test_real_preview_exit_publishes_one_terminal_event_and_rejects_late_readiness(
    monkeypatch, tmp_path: Path, exit_code: int,
) -> None:
    monkeypatch.setattr(launcher, "_RUNNING", {})

    async def scenario():
        exit_event = asyncio.Event()
        child = await spawn_exec(
            sys.executable,
            "-u",
            "-c",
            "import sys; print('http://127.0.0.1:43129', flush=True); "
            "sys.stdin.readline(); print('final diagnostic', file=sys.stderr); "
            f"sys.exit({exit_code})",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            on_exit=exit_event.set,
        )
        launched = _launch_fixture(tmp_path)
        launched.process = child
        launched._exit_event = exit_event
        launcher._RUNNING[launched.id] = launched
        events = []

        async def broadcast(event):
            events.append(event)

        monitor = asyncio.create_task(launcher._monitor_process(launched, broadcast))
        launched._monitor_task = monitor
        try:
            await asyncio.wait_for(launched.ready_event.wait(), timeout=5)
            child.stdin.write(b"exit\n")
            await child.stdin.drain()
            await asyncio.wait_for(monitor, timeout=5)
            terminal_type = "preview.launch.stopped" if exit_code == 0 else "preview.server.crashed"
            terminal_events = [event for event in events if event["type"] == terminal_type]
            assert len(terminal_events) == 1
            assert events[-1] == terminal_events[0]
            assert terminal_events[0]["conversation_id"] == launched.conversation_id
            assert terminal_events[0]["workspace_root"] == str(tmp_path)
            assert terminal_events[0]["stderr_tail"] == ["final diagnostic"]
            assert child.returncode == exit_code
            assert launched.id not in launcher._RUNNING
            assert not await launcher.mark_preview_ready(launched, broadcast)
            assert launched.status == ("exited" if exit_code == 0 else "crashed")
            assert len([event for event in events if event["type"] == "preview.server.ready"]) == 1
        finally:
            if child.returncode is None:
                child.kill()
            await child.wait()
            if not monitor.done():
                monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)
            launcher._RUNNING.pop(launched.id, None)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("status", "returncode", "accepted"),
    [
        ("starting", None, True),
        ("ready", None, True),
        ("stopping", None, False),
        ("exited", 0, False),
        ("crashed", 7, False),
        ("starting", 0, False),
        ("ready", 7, False),
    ],
)
def test_readiness_only_commits_for_a_live_preview(tmp_path: Path, status, returncode, accepted) -> None:
    async def scenario():
        launched = _launch_fixture(tmp_path)
        launched.status = status
        launched.process.returncode = returncode
        broadcast = AsyncMock()

        assert await launcher.mark_preview_ready(launched, broadcast) is accepted
        assert launched.status == ("ready" if accepted else status)
        assert launched.ready_event.is_set() is accepted
        assert broadcast.await_count == int(accepted and status != "ready")
        assert await launcher.mark_preview_ready(launched, broadcast) is accepted
        assert broadcast.await_count == int(accepted and status != "ready")

    asyncio.run(scenario())


def test_preview_stop_waits_for_monitor_and_stream_cancellation(monkeypatch, tmp_path: Path) -> None:
    async def scenario():
        launched = _launch_fixture(tmp_path)
        launched.process.stdout = asyncio.StreamReader()
        launched.process.stderr = asyncio.StreamReader()
        launched.process.stdout.feed_data(b"waiting output\n")
        launched.process.wait = AsyncMock(return_value=0)
        entered = asyncio.Event()
        finalized = asyncio.Event()

        async def broadcast(event):
            entered.set()
            try:
                await asyncio.Future()
            finally:
                await asyncio.sleep(0)
                finalized.set()

        async def terminate(process):
            process.returncode = 0
            return True

        monkeypatch.setattr(launcher, "_RUNNING", {launched.id: launched})
        monkeypatch.setattr(launcher, "terminate_process_tree", terminate)
        monitor = asyncio.create_task(launcher._monitor_process(launched, broadcast))
        launched._monitor_task = monitor
        try:
            await asyncio.wait_for(entered.wait(), timeout=1)
            stopped = await launcher.stop_preview_launch(
                session_id=launched.session_id,
                conversation_id=launched.conversation_id,
                workspace_root=tmp_path,
            )

            assert stopped == [launched]
            assert launched.status == "exited"
            assert monitor.done()
            assert finalized.is_set()
            assert not launcher._RUNNING
            assert not [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
        finally:
            if not monitor.done():
                monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize("exit_code", [0, 7], ids=["normal", "crash"])
def test_old_monitor_does_not_publish_a_terminal_event_for_a_replacement(
    monkeypatch, tmp_path: Path, exit_code: int,
) -> None:
    async def scenario():
        launched = _launch_fixture(tmp_path)
        launched.process.returncode = exit_code
        launched.process.stdout = None
        launched.process.stderr = None
        launched.process.wait = AsyncMock(return_value=exit_code)
        launched._exit_event.set()
        entered = asyncio.Event()
        release = asyncio.Event()

        async def terminate(process):
            entered.set()
            await release.wait()
            return True

        launched._sandbox_runner = SimpleNamespace(terminate=terminate)
        monkeypatch.setattr(launcher, "_RUNNING", {launched.id: launched})
        broadcast = AsyncMock()
        monitor = asyncio.create_task(launcher._monitor_process(launched, broadcast))
        try:
            await asyncio.wait_for(entered.wait(), timeout=1)
            replacement = _launch_fixture(tmp_path)
            launcher._RUNNING[replacement.id] = replacement
            release.set()
            await asyncio.wait_for(monitor, timeout=1)

            assert launcher._RUNNING[replacement.id] is replacement
            assert replacement.status == "starting"
            broadcast.assert_not_awaited()
        finally:
            release.set()
            await asyncio.gather(monitor, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize("completion", ["live", "exited", "crashed", "stopping"])
def test_preview_start_handler_does_not_accept_late_http_success(monkeypatch, tmp_path: Path, completion) -> None:
    async def scenario():
        launched = _launch_fixture(tmp_path)
        conversation = SimpleNamespace(id=launched.conversation_id, workspace_root=str(tmp_path), worktree_path="")
        session = SimpleNamespace(
            session_id=launched.session_id,
            active_conversation_id=conversation.id,
            conversation_repo=SimpleNamespace(get_conversation=lambda owner: conversation),
            session_lifecycle=SimpleNamespace(workspace_root=tmp_path, current_workspace_root=lambda: tmp_path),
            resolve_requested_workspace=lambda requested: resolve_requested_workspace(tmp_path, requested),
            send_event=AsyncMock(),
        )

        async def verify(url, **kwargs):
            if completion != "live":
                launched.status = completion
                launched.process.returncode = {"exited": 0, "crashed": 7, "stopping": None}[completion]
            return PreviewVerification(url, ok=True, status_code=200, elapsed_ms=5)

        monkeypatch.setattr("backend.preview.start_preview_launch", AsyncMock(return_value=launched))
        monkeypatch.setattr("backend.preview.verifier.wait_until_ready", verify)
        await handle_preview_launch_start(session, {
            "conversation_id": launched.conversation_id,
            "workspace_root": str(tmp_path),
            "request_id": "start-lifecycle",
        })

        verified = session.send_event.await_args.args[0]
        assert verified.type == "preview.verified"
        assert verified.data["ok"] is (completion == "live")
        assert verified.data["request_id"] == "start-lifecycle"
        assert verified.data["conversation_id"] == launched.conversation_id
        if completion != "live":
            assert "stopped" in verified.data["error"]
            assert launched.status == completion

    asyncio.run(scenario())


@pytest.mark.parametrize("static", [False, True], ids=["launch", "static"])
@pytest.mark.parametrize("verify", [False, True], ids=["immediate", "verification"])
@pytest.mark.parametrize("completion", ["live", "exited", "crashed", "stopping"])
def test_preview_tool_start_reports_the_current_process_state(
    monkeypatch, tmp_path: Path, static: bool, verify: bool, completion: str,
) -> None:
    async def scenario():
        launched = _launch_fixture(tmp_path)

        def complete():
            if completion != "live":
                launched.status = completion
                launched.process.returncode = {"exited": 0, "crashed": 7, "stopping": None}[completion]
                launched.stderr_tail.append("fixture diagnostic")

        async def start(*args, **kwargs):
            if not verify:
                complete()
            return launched

        async def wait_until_ready(url, **kwargs):
            complete()
            return PreviewVerification(url, ok=True, status_code=200, elapsed_ms=5)

        monkeypatch.setattr(launcher, "start_preview_launch", start)
        monkeypatch.setattr(launcher, "start_static_preview", start)
        monkeypatch.setattr("backend.preview.verifier.wait_until_ready", wait_until_ready)
        context = ToolExecutionContext(
            permission=PermissionContext(),
            session_id=launched.session_id,
            conversation_id=launched.conversation_id,
            workspace_root=tmp_path,
        )
        arguments = {"action": "start"}
        if static:
            arguments["path"] = "index.html"
        if verify:
            arguments["timeout"] = 1
        result = await PreviewServerTool().execute(arguments, context)

        assert result.is_error is (completion != "live")
        if completion == "live":
            assert json.loads(result.content)["status"] == ("ready" if verify else "starting")
        elif completion == "stopping":
            assert "stopped" in result.content
        else:
            assert f"code {launched.process.returncode}" in result.content
            assert "fixture diagnostic" in result.content

    asyncio.run(scenario())
