from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.preview import launcher, verifier
from backend.services.workspace_service import resolve_requested_workspace
from backend.ws.handlers.preview import handle_preview_launch_config, handle_preview_launch_start, handle_preview_launch_stop


def menu_session(tmp_path):
    conversation = SimpleNamespace(id="A", workspace_root=str(tmp_path), worktree_path="")
    return SimpleNamespace(
        session_id="session", active_conversation_id="A",
        conversation_repo=SimpleNamespace(get_conversation=lambda _: conversation),
        session_lifecycle=SimpleNamespace(workspace_root=tmp_path, current_workspace_root=lambda: tmp_path),
        resolve_requested_workspace=lambda value: resolve_requested_workspace(tmp_path, value),
        send_event=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_config_reply_returns_the_real_scoped_snapshot_and_semantic_result(tmp_path, monkeypatch):
    session = menu_session(tmp_path)
    config = launcher.PreviewLaunchConfig("web", "node server.js", str(tmp_path), 4173, "http://127.0.0.1:4173")
    monkeypatch.setattr("backend.preview.load_preview_launch_configs", lambda root: [config])
    running = lambda **owner: []
    monkeypatch.setattr("backend.preview.running_preview_processes", running)
    await handle_preview_launch_config(session, {"conversation_id": "A", "workspace_root": str(tmp_path), "client_command_id": "config-request"})
    snapshot, completed = [call.args[0] for call in session.send_event.await_args_list]
    assert snapshot.type == "preview.launch.config"
    assert completed.type == "command.result"
    assert completed.data["command"] == "preview.launch.config"
    assert completed.data["request_id"] == "config-request"
    assert completed.data["conversation_id"] == "A"
    assert completed.data["data"]["configs"] == [config.to_dict()]


@pytest.mark.asyncio
async def test_start_finishes_after_http_probe_and_retains_its_original_owner(tmp_path, monkeypatch):
    session = menu_session(tmp_path)
    process = launcher.PreviewLaunchProcess(
        id="web", config=launcher.PreviewLaunchConfig("web", "node server.js", str(tmp_path), 4173, "http://127.0.0.1:4173"),
        process=SimpleNamespace(pid=123, returncode=None), session_id="session", conversation_id="A", workspace_root=str(tmp_path),
    )
    monkeypatch.setattr(launcher, "_RUNNING", {process.id: process})
    monkeypatch.setattr("backend.preview.start_preview_launch", AsyncMock(return_value=process))
    waiting, release = asyncio.Event(), asyncio.Event()
    async def verify(*args, **kwargs):
        waiting.set()
        await release.wait()
        return verifier.PreviewVerification(process.effective_url, True, 200, 3)
    monkeypatch.setattr(verifier, "wait_until_ready", verify)
    task = asyncio.create_task(handle_preview_launch_start(session, {"conversation_id": "A", "workspace_root": str(tmp_path), "client_command_id": "start-request"}))
    await waiting.wait()
    assert not any(call.args[0].type == "command.result" for call in session.send_event.await_args_list)
    session.active_conversation_id = "B"
    release.set()
    await task
    result = session.send_event.await_args.args[0]
    assert result.type == "command.result" and result.data["level"] == "success"
    assert result.data["conversation_id"] == "A"
    assert result.data["request_id"] == "start-request"
    assert result.data["data"]["process"]["status"] == "ready"
    assert result.data["data"]["verification"]["status_code"] == 200


@pytest.mark.asyncio
async def test_stop_has_no_semantic_success_until_the_owned_process_is_confirmed_stopped(tmp_path, monkeypatch):
    session = menu_session(tmp_path)
    process = launcher.PreviewLaunchProcess(
        id="web", config=launcher.PreviewLaunchConfig("web", "node server.js", str(tmp_path), 4173, "http://127.0.0.1:4173"),
        process=SimpleNamespace(pid=123, returncode=0), status="exited", session_id="session", conversation_id="A", workspace_root=str(tmp_path),
    )
    waiting, release = asyncio.Event(), asyncio.Event()
    async def stop(name, **owner):
        assert name == "web"
        assert owner == {"session_id": "session", "conversation_id": "A", "workspace_root": str(tmp_path)}
        waiting.set()
        await release.wait()
        return [process]
    monkeypatch.setattr("backend.preview.stop_preview_launch", stop)
    task = asyncio.create_task(handle_preview_launch_stop(session, {"conversation_id": "A", "workspace_root": str(tmp_path), "name": "web", "client_command_id": "stop-request"}))
    await waiting.wait()
    assert session.send_event.await_count == 0
    release.set()
    await task
    stopped, completed = [call.args[0] for call in session.send_event.await_args_list]
    assert stopped.type == "preview.launch.stopped"
    assert completed.type == "command.result" and completed.data["level"] == "success"
    assert completed.data["request_id"] == "stop-request"
    assert completed.data["data"]["stopped"][0]["exit_code"] == 0
