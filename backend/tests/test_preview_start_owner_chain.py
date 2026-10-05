from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.preview import launcher, verifier
from backend.services.workspace_service import resolve_requested_workspace
from backend.services.preview_service import preview_launch_detected_event
from backend.tools.preview_tool import PreviewServerTool
from backend.ws.handlers.preview import handle_preview_launch_start


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["tool", "websocket"])
@pytest.mark.parametrize("completion", ["live", "unhealthy", "cleanup-pending", "replaced", "removed", "stopping", "exited"])
async def test_start_readiness_retains_the_original_process_and_request_owner(tmp_path, monkeypatch, surface, completion):
    process = launcher.PreviewLaunchProcess(
        id="start-owner", config=launcher.PreviewLaunchConfig("web", "controlled", str(tmp_path), 49123, "http://127.0.0.1:49123"),
        process=SimpleNamespace(pid=123, returncode=None), session_id="session", conversation_id="conversation-a", workspace_root=str(tmp_path),
    )
    monkeypatch.setattr(launcher, "_RUNNING", {process.id: process})
    monkeypatch.setattr(launcher, "start_preview_launch", AsyncMock(return_value=process))
    monkeypatch.setattr("backend.preview.start_preview_launch", AsyncMock(return_value=process))
    requested = asyncio.Event()
    release = asyncio.Event()

    async def http_reply(url, timeout):
        requested.set()
        await release.wait()
        return verifier.PreviewVerification(url, True, 200, 1)

    monkeypatch.setattr(verifier, "_verify_preview_url_http", http_reply)
    conversation = SimpleNamespace(id=process.conversation_id, workspace_root=str(tmp_path), worktree_path="")
    session = SimpleNamespace(
        session_id=process.session_id, active_conversation_id=process.conversation_id,
        conversation_repo=SimpleNamespace(get_conversation=lambda _: conversation),
        session_lifecycle=SimpleNamespace(workspace_root=tmp_path, current_workspace_root=lambda: tmp_path),
        resolve_requested_workspace=lambda requested: resolve_requested_workspace(tmp_path, requested),
        send_event=AsyncMock(),
    )
    context = ToolExecutionContext(permission=PermissionContext(), session_id=process.session_id, conversation_id=process.conversation_id, workspace_root=tmp_path)
    operation = PreviewServerTool().execute({"action": "start", "timeout": 1}, context) if surface == "tool" else handle_preview_launch_start(
        session, {"conversation_id": process.conversation_id, "workspace_root": str(tmp_path), "request_id": "start-1"},
    )
    task = asyncio.create_task(operation)
    try:
        await asyncio.wait_for(requested.wait(), timeout=2)
        if completion == "unhealthy":
            process.status = "unhealthy"
            process.cleanup_pending = True
        elif completion == "cleanup-pending":
            process.cleanup_pending = True
        elif completion == "replaced":
            launcher._RUNNING[process.id] = replace(process, process=SimpleNamespace(pid=124, returncode=None))
        elif completion == "removed":
            launcher._RUNNING.pop(process.id)
        elif completion == "stopping":
            process.status = "stopping"
        elif completion == "exited":
            process.status = "exited"
            process.process.returncode = 0
        session.active_conversation_id = "conversation-b"
        release.set()
        result = await asyncio.wait_for(task, timeout=2)
        if surface == "tool":
            assert result.is_error is (completion != "live")
            if completion == "live":
                assert json.loads(result.content)["status"] == "ready"
        else:
            event = session.send_event.await_args.args[0]
            assert event.data["conversation_id"] == "conversation-a"
            assert event.data["workspace_root"] == str(tmp_path)
            assert event.data["request_id"] == "start-1"
            if completion == "live":
                assert event.type == "command.result" and event.data["level"] == "success"
                assert event.data["data"]["verification"]["ok"] is True
                assert any(call.args[0].type == "preview.verified" for call in session.send_event.await_args_list)
            else:
                assert event.type == "command.result" and event.data["level"] == "error"
                assert not any(call.args[0].type == "preview.verified" for call in session.send_event.await_args_list)
        if completion == "replaced":
            assert launcher._RUNNING[process.id].process.pid == 124
            assert launcher._RUNNING[process.id].status == "starting"
            assert not await launcher.mark_preview_ready(process)
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_detected_preview_projection_uses_the_bound_port_and_url(tmp_path):
    process = launcher.PreviewLaunchProcess(
        id="detected", config=launcher.PreviewLaunchConfig("web", "controlled", str(tmp_path), 5173, "http://127.0.0.1:5173"),
        process=SimpleNamespace(pid=123, returncode=None), detected_url="http://127.0.0.1:5174/app", detected_port=5174,
    )
    event = preview_launch_detected_event(process)
    assert event.data["port"] == process.to_dict()["port"] == 5174
    assert event.data["url"] == process.to_dict()["url"] == "http://127.0.0.1:5174/app"
