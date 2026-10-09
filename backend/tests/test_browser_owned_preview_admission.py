from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.preview import launcher
from backend.sandbox import SandboxPolicy
from backend.tools.browser_control_tool import BrowserControlTool
from backend.tools.browser_support import _owned_preview_navigation_authorization


@pytest.fixture
def owned_preview(tmp_path, monkeypatch):
    preview = launcher.PreviewLaunchProcess(
        id="live-preview", config=launcher.PreviewLaunchConfig(
            "web", "controlled", str(tmp_path), 49123, "http://127.0.0.1:49123/token/index.html",
        ),
        process=SimpleNamespace(pid=123, returncode=None), status="ready",
        session_id="session", conversation_id="conversation", workspace_root=str(tmp_path),
    )
    monkeypatch.setattr(launcher, "_RUNNING", {preview.id: preview})
    context = ToolExecutionContext(
        permission=PermissionContext(mode="bypass"), session_id=preview.session_id,
        conversation_id=preview.conversation_id, workspace_root=tmp_path,
    )
    return preview, context


@pytest.mark.parametrize("mismatch", [
    "session", "conversation", "workspace", "no-workspace", "other-origin", "removed", "stopped", "exited", "cleanup-pending",
])
def test_preview_authority_requires_the_current_live_owner(owned_preview, tmp_path, mismatch):
    preview, context = owned_preview
    url = preview.effective_url
    if mismatch == "session":
        context = replace(context, session_id="another-session")
    elif mismatch == "conversation":
        context = replace(context, conversation_id="another-conversation")
    elif mismatch == "workspace":
        context = replace(context, workspace_root=tmp_path / "other")
    elif mismatch == "no-workspace":
        context = replace(context, workspace_root=None)
    elif mismatch == "other-origin":
        url = "http://127.0.0.1:8000/admin"
    elif mismatch == "removed":
        launcher._RUNNING.pop(preview.id)
    elif mismatch == "stopped":
        preview.status = "stopped"
    elif mismatch == "exited":
        preview.process.returncode = 0
    else:
        preview.cleanup_pending = True
    assert _owned_preview_navigation_authorization(url, context) is None


@pytest.mark.asyncio
async def test_browser_bridge_gets_runtime_authority_instead_of_model_payload(owned_preview, monkeypatch):
    preview, context = owned_preview
    requests = []

    async def receive(request):
        import json
        payload = json.loads(request.content)
        requests.append((request.headers, payload))
        return httpx.Response(200, json={"ok": True, "target": {"id": "page", "url": payload["url"]}})

    client = httpx.AsyncClient
    monkeypatch.setattr("backend.tools.browser_control_tool.httpx.AsyncClient", lambda **kwargs: client(
        **kwargs, transport=httpx.MockTransport(receive),
    ))
    monkeypatch.setenv("MINICODE_EMBEDDED_BROWSER_ENDPOINT", "http://127.0.0.1:43123")
    monkeypatch.setenv("MINICODE_EMBEDDED_BROWSER_TOKEN", "backend-only-token")
    result = await BrowserControlTool().execute({
        "action": " NAVIGATE ", "url": preview.effective_url,
        "conversation_id": "forged", "operation_id": "forged",
        "navigation_authorization": {"kind": "bypass", "session_id": "forged"},
    }, context)
    assert not result.is_error
    assert len(requests) == 1
    headers, payload = requests[0]
    assert headers["authorization"] == "Bearer backend-only-token"
    assert payload["conversation_id"] == context.conversation_id
    assert payload["action"] == "navigate"
    assert payload["operation_id"].startswith("browser_")
    assert payload["navigation_authorization"] == {
        "kind": "owned_preview", "url": preview.effective_url,
        "preview_url": preview.effective_url, "preview_id": preview.id,
        "session_id": context.session_id, "conversation_id": context.conversation_id,
        "permission_mode": "bypass", "operation_id": payload["operation_id"],
    }


def test_metadata_origin_does_not_forge_a_registered_preview(tmp_path):
    context = ToolExecutionContext(
        permission=PermissionContext(mode="bypass"), session_id="metadata-session",
        conversation_id="metadata-conversation", workspace_root=tmp_path,
        metadata={"preview_origins": ["http://127.0.0.1:8000"]},
    )
    assert _owned_preview_navigation_authorization("http://127.0.0.1:8000/admin", context) is None


def test_browser_admission_token_never_reaches_tool_subprocesses(monkeypatch):
    from backend.runtime_env import mcp_subprocess_env, sanitized_subprocess_env, shell_subprocess_env

    name = "MINICODE_EMBEDDED_BROWSER_TOKEN"
    monkeypatch.setenv(name, "backend-only-token")
    environments = [
        shell_subprocess_env({"inherit": "all", "ignore_default_excludes": True}),
        mcp_subprocess_env(inherited_names=(name,)),
        sanitized_subprocess_env(allow={name}),
    ]
    assert all(name not in environment for environment in environments)


@pytest.mark.asyncio
async def test_workspace_html_launch_is_the_same_registered_preview_authorized_for_navigation(tmp_path):
    from backend.tools.browser_support import _resolved_navigation_url

    page = tmp_path / "index.html"
    page.write_text("<!doctype html><title>owned preview</title>", encoding="utf-8")
    context = ToolExecutionContext(
        permission=PermissionContext(mode="bypass"), session_id="real-browser-preview-session",
        conversation_id="real-browser-preview-conversation", workspace_root=tmp_path,
        sandbox_policy=SandboxPolicy.bypass(),
    )
    try:
        url, error = await _resolved_navigation_url("index.html", context)
        assert error == ""
        preview = launcher.find_preview_process(
            url, session_id=context.session_id, conversation_id=context.conversation_id, workspace_root=tmp_path,
        )
        assert preview is not None
        await asyncio.wait_for(preview.ready_event.wait(), timeout=10)
        async with httpx.AsyncClient(trust_env=False) as client:
            response = await client.get(url)
        assert response.status_code == 200
        assert "<title>owned preview</title>" in response.text
        authorization = _owned_preview_navigation_authorization(url, context)
        assert authorization["preview_id"] == preview.id
        assert authorization["url"] == authorization["preview_url"] == url
        assert authorization["session_id"] == context.session_id
        assert authorization["conversation_id"] == context.conversation_id
        await launcher.stop_preview_launch(session_id=context.session_id, conversation_id=context.conversation_id)
        assert _owned_preview_navigation_authorization(url, context) is None
    finally:
        await launcher.stop_preview_launch(session_id=context.session_id, conversation_id=context.conversation_id)
