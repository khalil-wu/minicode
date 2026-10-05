from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from backend.api import routes_github
from backend.services import github_service, workspace_service
from backend.workspace import delivery


@pytest.fixture(autouse=True)
def github_host(monkeypatch):
    monkeypatch.delenv("GH_HOST", raising=False)
    monkeypatch.delenv("MINICODE_GH_COMMAND", raising=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("accounts, authenticated", [
    ([], False),
    ([{"state": "failure", "active": True, "login": "expired", "token": "secret"}], False),
    ([{"state": "success", "active": True, "login": "connected-user", "token": "secret"}], True),
])
async def test_connection_status_uses_actual_account_state_and_never_publishes_a_token(monkeypatch, accounts, authenticated):
    monkeypatch.setattr(github_service, "github_cli_command", lambda: "managed-gh")
    spawn = AsyncMock(return_value=SimpleNamespace(returncode=0))
    monkeypatch.setattr(github_service, "spawn_exec", spawn)
    monkeypatch.setattr(github_service, "communicate", AsyncMock(return_value=(json.dumps({"hosts": {"github.com": accounts}}).encode(), b"")))
    result = await github_service.github_connection_status()
    assert result["available"] is True
    assert result["authenticated"] is authenticated
    assert "secret" not in json.dumps(result)
    assert "token" not in result
    assert spawn.await_args.args[0] == "managed-gh"


@pytest.mark.asyncio
async def test_missing_runtime_is_a_real_capability_state_and_login_does_not_spawn(monkeypatch):
    monkeypatch.setattr(github_service, "github_cli_command", lambda: None)
    monkeypatch.setattr(routes_github, "github_cli_command", lambda: None)
    spawn = AsyncMock()
    monkeypatch.setattr(github_service, "spawn_exec", spawn)
    assert (await github_service.github_connection_status())["available"] is False
    with pytest.raises(HTTPException, match="GitHub") as error:
        await routes_github.github_login()
    assert error.value.status_code == 503
    spawn.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("output", [b"<html>proxy</html>", b"{}", b'{"hosts":[]}'])
async def test_a_proxy_or_broken_cli_response_is_not_reported_as_connected(monkeypatch, output):
    monkeypatch.setattr(github_service, "github_cli_command", lambda: "gh")
    monkeypatch.setattr(github_service, "spawn_exec", AsyncMock(return_value=SimpleNamespace(returncode=0)))
    monkeypatch.setattr(github_service, "communicate", AsyncMock(return_value=(output, b"")))
    with pytest.raises(github_service.GitHubCommandError):
        await github_service.github_connection_status()
    with pytest.raises(HTTPException) as error:
        await routes_github.github_status()
    assert error.value.status_code == 502


class LoginProcess:
    def __init__(self, *, ended=False, output=b"! First copy your one-time code: ABCD-1234\nOpen this URL to continue in your web browser: https://github.com/login/device\n"):
        self.returncode = None
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_data(output)
        if ended:
            self.stdout.feed_eof()

    async def wait(self):
        self.returncode = 0
        return 0


@pytest.mark.asyncio
@pytest.mark.parametrize("authenticated", [False, True])
async def test_browser_authorization_reports_code_then_only_a_verified_connection(monkeypatch, authenticated):
    process = LoginProcess(ended=True)
    monkeypatch.setattr(github_service, "spawn_exec", AsyncMock(return_value=process))
    connection = {"available": True, "authenticated": authenticated, "host": "github.com", "login": "user" if authenticated else None, "message": ""}
    monkeypatch.setattr(github_service, "github_connection_status", AsyncMock(return_value=connection))
    events = [event async for event in github_service.github_login_events("managed-gh")]
    assert events[1]["phase"] == "authorizing"
    assert events[1]["user_code"] == "ABCD-1234"
    assert "verification_uri" not in events[1]
    assert events[2]["verification_uri"] == "https://github.com/login/device"
    arguments = github_service.spawn_exec.await_args
    assert arguments.args[-1] == "--clipboard=false"
    assert arguments.kwargs["stdin"] == asyncio.subprocess.DEVNULL
    assert events[-1]["phase"] == ("connected" if authenticated else "error")
    if authenticated:
        assert events[-1]["connection"] == connection


@pytest.mark.asyncio
async def test_cancelled_authorization_releases_its_owned_process(monkeypatch):
    process = LoginProcess()
    monkeypatch.setattr(github_service, "spawn_exec", AsyncMock(return_value=process))
    terminate = AsyncMock(return_value=True)
    monkeypatch.setattr(github_service, "terminate_process_tree", terminate)
    events = github_service.github_login_events("managed-gh")
    assert (await anext(events))["phase"] == "starting"
    await events.aclose()
    terminate.assert_awaited_once_with(process)


@pytest.mark.asyncio
async def test_enterprise_web_flow_publishes_the_actual_authorization_url_without_a_device_code(monkeypatch):
    host = "github.company.example"
    target = f"https://{host}/login/oauth/authorize?client_id=fixture&state=opaque"
    monkeypatch.setenv("GH_HOST", host)
    process = LoginProcess(ended=True, output=f"Open this URL to continue in your web browser: {target}\n".encode())
    monkeypatch.setattr(github_service, "spawn_exec", AsyncMock(return_value=process))
    monkeypatch.setattr(github_service, "github_connection_status", AsyncMock(return_value={"available": True, "authenticated": True, "host": host, "login": "user", "message": ""}))
    events = [event async for event in github_service.github_login_events("managed-gh")]
    authorization = next(event for event in events if event["phase"] == "authorizing")
    assert authorization["verification_uri"] == target
    assert "user_code" not in authorization
    assert events[-1]["phase"] == "connected"


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["file://github.com/login/device", "https://different.example/login/device", "https://[/invalid"])
async def test_invalid_cli_authorization_urls_are_rejected_at_the_output_boundary(monkeypatch, target):
    process = LoginProcess(ended=True, output=f"Open this URL to continue in your web browser: {target}\n".encode())
    monkeypatch.setattr(github_service, "spawn_exec", AsyncMock(return_value=process))
    terminate = AsyncMock(return_value=True)
    monkeypatch.setattr(github_service, "terminate_process_tree", terminate)
    with pytest.raises(github_service.GitHubCommandError, match="invalid authorization URL"):
        _ = [event async for event in github_service.github_login_events("managed-gh")]
    terminate.assert_awaited_once_with(process)


@pytest.mark.asyncio
async def test_asgi_disconnect_while_sending_an_event_closes_both_generators_and_owned_cli(monkeypatch):
    process = LoginProcess()
    monkeypatch.setattr(routes_github, "github_cli_command", lambda: "managed-gh")
    monkeypatch.setattr(github_service, "spawn_exec", AsyncMock(return_value=process))
    terminate = AsyncMock(return_value=True)
    monkeypatch.setattr(github_service, "terminate_process_tree", terminate)
    response = await routes_github.github_login()
    disconnected = asyncio.Event()

    async def send(message):
        if message["type"] == "http.response.body":
            disconnected.set()
            await asyncio.Event().wait()

    async def receive():
        await disconnected.wait()
        return {"type": "http.disconnect"}

    await asyncio.wait_for(response({"type": "http", "asgi": {"spec_version": "2.0"}}, receive, send), timeout=2)
    terminate.assert_awaited_once_with(process)
    assert response.body_iterator.ag_frame is None


@pytest.mark.asyncio
@pytest.mark.parametrize("remote, eligible", [("", False), ("https://gitlab.com/group/project.git", False), ("git@github.com:group/project.git", True)])
async def test_local_repository_capabilities_are_checked_before_github_runtime(tmp_path, monkeypatch, remote, eligible):
    subprocess.run(["git", "init", "-q", "-b", "feature"], cwd=tmp_path, check=True)
    if remote:
        subprocess.run(["git", "remote", "add", "origin", remote], cwd=tmp_path, check=True)
    context = await github_service.github_repository_context(tmp_path)
    assert context["eligible"] is eligible
    assert context["branch"] == "feature"
    if not eligible:
        cli = AsyncMock(side_effect=AssertionError("non-GitHub repository must not query GitHub"))
        monkeypatch.setattr(workspace_service, "_run_gh_pr_view", cli)
        result = await workspace_service.fetch_git_pr_status_payload(tmp_path)
        assert "error" not in result and result["pr"] is None
        cli.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["head", "timeout", "repository"])
async def test_repository_metadata_failures_are_not_projected_as_detached_or_non_git(tmp_path, monkeypatch, failure):
    def metadata(root, *args, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired("git", 5)
        if args[0] == "rev-parse":
            return subprocess.CompletedProcess(args, 3 if failure == "repository" else 0, "true\n", "proxy: not a git repository" if failure == "repository" else "")
        if args[0] == "config":
            return subprocess.CompletedProcess(args, 0, "remote.origin.url https://github.com/group/project.git\n", "")
        return subprocess.CompletedProcess(args, 128, "", "fatal: invalid HEAD")

    monkeypatch.setattr("backend.services.workspace_api_service.run_ui_git_metadata", metadata)
    with pytest.raises(github_service.GitHubCommandError):
        await github_service.github_repository_context(tmp_path)


@pytest.mark.asyncio
async def test_detached_head_is_a_repository_without_an_active_pr_branch(tmp_path):
    subprocess.run(["git", "init", "-q", "-b", "feature"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "--allow-empty", "-qm", "initial"], cwd=tmp_path, check=True)
    subprocess.run(["git", "checkout", "--detach", "-q", "HEAD"], cwd=tmp_path, check=True)
    subprocess.run(["git", "remote", "add", "origin", "https://github.com/group/project.git"], cwd=tmp_path, check=True)
    context = await github_service.github_repository_context(tmp_path)
    assert context == {"is_git_repo": True, "eligible": True, "host": "github.com", "branch": ""}


@pytest.mark.asyncio
async def test_local_git_delivery_survives_an_unavailable_optional_github_connection(tmp_path, monkeypatch):
    await delivery.git_delivery_action(tmp_path, delivery.GitDeliveryRequest(action="init"))
    subprocess.run(["git", "remote", "add", "origin", "https://github.com/group/project.git"], cwd=tmp_path, check=True)
    monkeypatch.setattr(delivery, "github_cli_command", lambda: "managed-gh")
    monkeypatch.setattr(delivery, "github_connection_status", AsyncMock(side_effect=github_service.GitHubCommandError("network unavailable")))
    result = await delivery.git_delivery_status(tmp_path)
    assert result["is_git_repo"] is True and result["branch"] == "main"
    assert result["github"]["authenticated"] is None
    with pytest.raises(HTTPException) as error:
        await delivery.git_delivery_action(tmp_path, delivery.GitDeliveryRequest(action="draft_pr", expected_branch="main", title="Test", base="main"))
    assert error.value.status_code == 502
