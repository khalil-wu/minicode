from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.preview import launcher, verifier
from backend.services.workspace_service import resolve_requested_workspace
from backend.tools.preview_tool import PreviewServerTool
from backend.ws.handlers.preview import handle_preview_navigate, handle_preview_verify


def _launch(workspace: Path) -> launcher.PreviewLaunchProcess:
    return launcher.PreviewLaunchProcess(
        id="verify-lifecycle",
        config=launcher.PreviewLaunchConfig("web", "fixture", str(workspace), 43136, "http://127.0.0.1:43136"),
        process=SimpleNamespace(pid=1234, returncode=None), status="ready",
        session_id="verify-session", conversation_id="verify-conversation", workspace_root=str(workspace),
    )


def _session(launched: launcher.PreviewLaunchProcess) -> SimpleNamespace:
    workspace = Path(launched.workspace_root)
    return SimpleNamespace(
        session_id=launched.session_id, active_conversation_id=launched.conversation_id,
        session_lifecycle=SimpleNamespace(workspace_root=workspace, current_workspace_root=lambda: workspace),
        resolve_requested_workspace=lambda requested: resolve_requested_workspace(workspace, requested),
        send_event=AsyncMock(),
    )


@pytest.mark.parametrize("command", ["preview.navigate", "preview.verify", "tool.verify"])
@pytest.mark.parametrize("completion", ["live", "exited", "stopping", "replaced", "removed", "cleanup-pending"])
def test_verification_result_is_bound_to_the_original_process_instance(
    monkeypatch, tmp_path: Path, command: str, completion: str,
) -> None:
    async def scenario():
        launched = _launch(tmp_path)
        monkeypatch.setattr(launcher, "_RUNNING", {launched.id: launched})
        requested = asyncio.Event()
        release = asyncio.Event()

        async def http_result(url, timeout):
            requested.set()
            await release.wait()
            return verifier.PreviewVerification(url, ok=True, status_code=200, elapsed_ms=5)

        monkeypatch.setattr(verifier, "_verify_preview_url_http", http_result)
        session = _session(launched)
        if command == "tool.verify":
            context = ToolExecutionContext(
                permission=PermissionContext(), session_id=launched.session_id,
                conversation_id=launched.conversation_id, workspace_root=tmp_path,
            )
            operation = PreviewServerTool().execute({"action": "verify", "url": launched.effective_url}, context)
        else:
            handler = handle_preview_navigate if command == "preview.navigate" else handle_preview_verify
            operation = handler(session, {"url": launched.effective_url, "request_id": "verify-request"})
        task = asyncio.create_task(operation)
        try:
            await asyncio.wait_for(requested.wait(), timeout=2)
            if completion == "exited":
                launched.process.returncode = 0
                launched.status = "exited"
            elif completion == "stopping":
                launched.status = "stopping"
            elif completion == "replaced":
                launcher._RUNNING[launched.id] = replace(launched, process=SimpleNamespace(pid=1235, returncode=None))
            elif completion == "removed":
                launcher._RUNNING.pop(launched.id)
            elif completion == "cleanup-pending":
                launched.cleanup_pending = True
            session.active_conversation_id = "another-conversation"
            release.set()
            result = await asyncio.wait_for(task, timeout=2)

            if command == "tool.verify":
                assert result.is_error is (completion != "live")
                if completion == "live":
                    assert json.loads(result.content)["ok"] is True
                else:
                    assert "stopped or restarted" in result.content
            else:
                event = session.send_event.await_args.args[0]
                assert event.data["conversation_id"] == launched.conversation_id
                assert event.data["workspace_root"] == str(tmp_path)
                assert event.data["request_id"] == "verify-request"
                if completion == "live":
                    assert event.type == "preview.verified"
                    assert event.data["ok"] is True
                else:
                    assert event.type == "command.result"
                    assert event.data["command"] == command
                    assert event.data["level"] == "error"
                    assert "stopped or restarted" in event.data["message"]
                    assert not any(call.args[0].type == "preview.verified" for call in session.send_event.await_args_list)
            if completion == "replaced":
                assert launcher._RUNNING[launched.id].is_active
                assert launcher._RUNNING[launched.id].process.pid == 1235
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_navigation_stopped_before_http_does_not_access_the_old_url(monkeypatch, tmp_path: Path) -> None:
    async def scenario():
        launched = _launch(tmp_path)
        monkeypatch.setattr(launcher, "_RUNNING", {launched.id: launched})
        http = AsyncMock()
        monkeypatch.setattr(verifier, "_verify_preview_url_http", http)
        session = _session(launched)

        async def stop_on_navigation(event):
            if event.type == "preview.navigated":
                launched.status = "stopping"

        session.send_event.side_effect = stop_on_navigation
        await handle_preview_navigate(session, {"url": launched.effective_url, "request_id": "navigation-stopped"})

        http.assert_not_awaited()
        assert session.send_event.await_args.args[0].type == "command.result"
        assert session.send_event.await_args.args[0].data["request_id"] == "navigation-stopped"

    asyncio.run(scenario())


def test_verification_without_a_launch_keeps_the_existing_http_contract(monkeypatch) -> None:
    result = verifier.PreviewVerification("https://example.test/", ok=False, status_code=503, elapsed_ms=3, error="HTTP 503")
    http = AsyncMock(return_value=result)
    monkeypatch.setattr(verifier, "_verify_preview_url_http", http)

    assert asyncio.run(verifier.verify_preview_url(result.url, timeout=2)) is result
    http.assert_awaited_once_with(result.url, 2)


def test_process_lookup_preserves_owner_and_origin_boundaries(monkeypatch, tmp_path: Path) -> None:
    launched = _launch(tmp_path)
    monkeypatch.setattr(launcher, "_RUNNING", {launched.id: launched})
    owner = {"session_id": launched.session_id, "conversation_id": launched.conversation_id, "workspace_root": tmp_path}

    assert launcher.find_preview_process(f"{launched.effective_url}/app?tab=2", **owner) is launched
    assert launcher.preview_url_is_owned(launched.effective_url, **owner)
    assert launcher.find_preview_process(launched.effective_url, **{**owner, "conversation_id": "other"}) is None
    assert launcher.find_preview_process(launched.effective_url, **{**owner, "workspace_root": tmp_path / "other"}) is None
    for url in ("https://127.0.0.1:43136", "http://127.0.0.1:43137", "http://[::1", "http://localhost:invalid"):
        assert launcher.find_preview_process(url, **owner) is None
        assert not launcher.preview_url_is_owned(url, **owner)
    assert launcher.preview_url_is_owned("http://localhost:43137/app", extra_urls=("http://localhost:43137",))
    assert not launcher.preview_url_is_owned("http://localhost:invalid", extra_urls=("http://localhost:invalid",))
