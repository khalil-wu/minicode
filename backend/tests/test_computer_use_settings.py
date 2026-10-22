"""Real native persistent approvals and provider-aware settings, with no desktop API calls."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import os
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
import httpx
import pytest

from backend.api import _state
from backend.api.routes_computer_use import router
from backend.mcp.cua_approval_store import NativeAppApprovalStore, native_approval_home
from backend.mcp.manager import MCPServerConfig
from backend.mcp.native_cua_runtime import PROVIDER
from backend.mcp.native_cua_windows import NativeCuaSession
from backend.tests.fixtures.native_cua_receiving import FixtureDesktop
from backend.tests.test_cua_repl_companion import selected_runtime
from backend.tests.test_native_cua_runtime import native_companion, text, OWNER


def settings_client(monkeypatch, workspace, selected, *, conversation_id="real-thread"):
    import backend.services.computer_use_settings as service
    state_root = Path(os.environ["MINICODE_STATE_ROOT"])
    monkeypatch.setattr("backend.config.STATE_ROOT", state_root)
    monkeypatch.setattr("backend.commands.plugins.STATE_ROOT", state_root)
    monkeypatch.setattr(service, "os", SimpleNamespace(name="nt"))
    choices = [selected] if selected is not None else []
    session = SimpleNamespace(active_conversation_id=conversation_id,
        conversation_repo=SimpleNamespace(store_instance_id=lambda: "original-store"),
        session_lifecycle=SimpleNamespace(workspace_root_for_conversation=lambda: workspace),
        _mcp_manager_for_workspace=lambda root: SimpleNamespace(load_config=lambda: list(choices)))
    local_thread = SimpleNamespace(thread_environments=SimpleNamespace(selections=lambda: (
        SimpleNamespace(environment_id="local"),)), cua_repl_companions=None)
    contexts = SimpleNamespace(thread_runtime_for=lambda store, conversation: local_thread)
    monkeypatch.setattr(_state, "ws_manager", SimpleNamespace(get_session=lambda value: session if value == "settings-session" else None,
        execution_contexts=contexts))
    app = FastAPI()
    app.include_router(router)
    query = {"session_id": "settings-session", "workspace_root": str(workspace)}
    if conversation_id:
        query["conversation_id"] = conversation_id
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test")
    return client, query, session, choices


def native_config():
    import shutil
    return MCPServerConfig("cua_repl", command=shutil.which("node"), env={
        "MINICODE_CUA_PROVIDER": PROVIDER, "CUA_REPL_ENABLED_SURFACES": "computer"})


def test_native_store_preserves_other_apps_under_concurrent_grants_and_revoke(tmp_path):
    store = NativeAppApprovalStore(tmp_path)
    identity = FixtureDesktop().current_identity
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda index: store.grant({**identity, "app_id": str(index), "name": f"App {index}"}), range(12)))
    assert len(store.list_apps()) == 12
    assert len(store.remove("3")) == 11
    assert store.get("3") is None and store.get("4")["display_name"] == "App 4"


@pytest.mark.parametrize("change", ["removed", "corrupt"])
def test_live_always_target_never_defaults_to_allowed_after_store_disappears_or_breaks(tmp_path, change):
    store = NativeAppApprovalStore(tmp_path)
    desktop = FixtureDesktop()
    store.grant(desktop.current_identity)
    session = NativeCuaSession(desktop, store)
    args = {"window_id": "11", "target": "original-target", "scope": "always"}
    session.dispatch("select_window", args, OWNER)
    session.dispatch("authorize", args, OWNER)
    if change == "removed":
        store.path.unlink()
        expected, message = PermissionError, "approval was revoked"
    else:
        store.path.write_text("invalid-json")
        expected, message = json.JSONDecodeError, "Expecting value"
    with pytest.raises(expected, match=message):
        session.dispatch("get_state", args, OWNER)
    assert desktop.operations == []
    session.close()


def test_capture_result_is_rejected_when_permanent_approval_is_revoked_during_capture(tmp_path):
    store = NativeAppApprovalStore(tmp_path)
    desktop = FixtureDesktop()
    store.grant(desktop.current_identity)
    session = NativeCuaSession(desktop, store)
    args = {"window_id": "11", "target": "original-target", "scope": "always"}
    session.dispatch("select_window", args, OWNER)
    session.dispatch("authorize", args, OWNER)
    capture = desktop.capture
    def revoked_capture(hwnd):
        image = capture(hwnd)
        store.remove("fixture.editor")
        return image
    desktop.capture = revoked_capture
    with pytest.raises(PermissionError, match="approval was revoked"):
        session.dispatch("screenshot", args, OWNER)
    session.close()


@pytest.mark.asyncio
async def test_home_settings_read_real_native_store_and_return_fresh_revoke_list(tmp_path, monkeypatch):
    store = NativeAppApprovalStore(native_approval_home())
    store.grant(FixtureDesktop().current_identity)
    client, query, _, _ = settings_client(monkeypatch, tmp_path, native_config(), conversation_id=None)
    async with client:
        response = await client.get("/api/settings/computer-use", params=query)
        assert response.status_code == 200
        snapshot = response.json()
        assert snapshot["navigation_visible"] and snapshot["available"]
        assert snapshot["capabilities"]["persist_modes"] == ["session", "always"]
        assert snapshot["controls"] == [] and snapshot["owner"]["conversation_id"] is None
        assert snapshot["approved_apps"] == [{"app_id": "fixture.editor", "display_name": "Fixture Editor", "icon_data_url": None}]
        removed = await client.post("/api/settings/computer-use/app-approvals/remove", json={**query,
            "provider_id": snapshot["provider_id"], "selection_id": snapshot["selection_id"], "app_id": "fixture.editor"})
        assert removed.status_code == 200 and removed.json()["approved_apps"] == []
        assert store.get("fixture.editor") is None


@pytest.mark.asyncio
async def test_stale_owner_or_provider_cannot_revoke_the_original_store(tmp_path, monkeypatch):
    store = NativeAppApprovalStore(native_approval_home())
    store.grant(FixtureDesktop().current_identity)
    client, query, session, choices = settings_client(monkeypatch, tmp_path, native_config())
    async with client:
        snapshot = (await client.get("/api/settings/computer-use", params=query)).json()
        body = {**query, "provider_id": snapshot["provider_id"], "selection_id": snapshot["selection_id"], "app_id": "fixture.editor"}
        session.active_conversation_id = "another-thread"
        assert (await client.post("/api/settings/computer-use/app-approvals/remove", json=body)).status_code == 409
        session.active_conversation_id = "real-thread"
        choices[:] = [replace(choices[0], enabled=False)]
        assert (await client.post("/api/settings/computer-use/app-approvals/remove", json=body)).status_code == 409
        assert store.get("fixture.editor") is not None
        assert (await client.get("/api/settings/computer-use", params={**query, "session_id": "foreign"})).status_code == 404


@pytest.mark.asyncio
async def test_unverified_installed_contract_and_remote_never_read_or_modify_another_home(tmp_path, monkeypatch):
    selected = selected_runtime(tmp_path / "installed")
    client, query, _, choices = settings_client(monkeypatch, tmp_path, selected)
    private = native_approval_home().parent / "computer-use/config.toml"
    private.parent.mkdir(parents=True)
    private.write_text('[computer_use.windows.always_allowed_app_ids]\n"private-app" = true\n')
    async with client:
        snapshot = (await client.get("/api/settings/computer-use", params=query)).json()
        assert snapshot["available"] and not snapshot["navigation_visible"]
        assert snapshot["approved_apps"] is None and not snapshot["capabilities"]["persistent_approvals"]
        response = await client.post("/api/settings/computer-use/app-approvals/remove", json={**query,
            "provider_id": snapshot["provider_id"], "selection_id": snapshot["selection_id"], "app_id": "private-app"})
        assert response.status_code == 503
        assert '"private-app" = true' in private.read_text()
        choices[:] = [replace(native_config(), environment_id="remote-windows")]
        remote = (await client.get("/api/settings/computer-use", params=query)).json()
        assert remote["status"] == "unsupported" and remote["approved_apps"] is None
        assert not remote["capabilities"]["revoke_app_approval"]


@pytest.mark.asyncio
async def test_policy_disabled_unconfigured_and_corrupt_store_are_distinct(tmp_path, monkeypatch):
    client, query, _, choices = settings_client(monkeypatch, tmp_path, None)
    async with client:
        missing = (await client.get("/api/settings/computer-use", params=query)).json()
        assert missing["status"] == "not_configured" and not missing["navigation_visible"]
        choices[:] = [replace(native_config(), enabled=False, disabled_reason="Blocked by MiniCode MCP policy")]
        denied = (await client.get("/api/settings/computer-use", params=query)).json()
        assert denied["status"] == "policy_denied" and denied["navigation_visible"]
        choices[:] = [native_config()]
        path = NativeAppApprovalStore(native_approval_home()).path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("invalid-json")
        assert (await client.get("/api/settings/computer-use", params=query)).status_code == 500


@pytest.mark.asyncio
async def test_existing_remote_thread_selection_never_uses_a_local_raw_config_store(tmp_path, monkeypatch):
    store = NativeAppApprovalStore(native_approval_home())
    store.grant(FixtureDesktop().current_identity)
    client, query, _, _ = settings_client(monkeypatch, tmp_path, native_config())
    async with client:
        local = (await client.get("/api/settings/computer-use", params=query)).json()
        remote_thread = SimpleNamespace(thread_environments=SimpleNamespace(selections=lambda: (
            SimpleNamespace(environment_id="remote-windows"),)), cua_repl_companions=None)
        _state.ws_manager.execution_contexts.thread_runtime_for = lambda saved_store, thread_id: remote_thread
        remote = (await client.get("/api/settings/computer-use", params=query)).json()
        assert remote["status"] == "unsupported" and remote["approved_apps"] is None
        assert remote["selection_id"] != local["selection_id"]
        stale = await client.post("/api/settings/computer-use/app-approvals/remove", json={**query,
            "provider_id": local["provider_id"], "selection_id": local["selection_id"], "app_id": "fixture.editor"})
        assert stale.status_code == 409 and store.get("fixture.editor") is not None


@pytest.mark.asyncio
async def test_cold_conversation_without_receiving_authority_does_not_infer_local_from_config(tmp_path, monkeypatch):
    client, query, _, _ = settings_client(monkeypatch, tmp_path, native_config())
    _state.ws_manager.execution_contexts.thread_runtime_for = lambda store, conversation: None
    path = NativeAppApprovalStore(native_approval_home()).path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("must not be read without original receiving authority")
    async with client:
        snapshot = (await client.get("/api/settings/computer-use", params=query)).json()
        assert snapshot["status"] == "unsupported" and "not captured" in snapshot["reason"]
        assert snapshot["approved_apps"] is None and not snapshot["navigation_visible"]


@pytest.mark.asyncio
async def test_original_always_approval_is_persisted_consumed_by_new_client_and_revoked_live(tmp_path, monkeypatch):
    owner = {"session_id": "receiving-session", "conversation_id": OWNER["session_id"], "turn_id": OWNER["turn_id"]}
    async with native_companion(tmp_path, monkeypatch, choice={"action": "accept", "content": {"persist": "always"}}) as first:
        chosen = await first.client.call_tool("js", {"code": "var app = await cua.getApp({window_id:'11'}); await app.getState();"}, request_owner=owner)
        assert not chosen.is_error, text(chosen)
        assert len(first.approvals) == 1 and "always" in first.approvals[0]["_meta"]["persist"]
        store = NativeAppApprovalStore(native_approval_home())
        assert store.get("fixture.editor")["display_name"] == "Fixture Editor"
        other = tmp_path / "second-client"
        other.mkdir()
        async with native_companion(other, monkeypatch, choice={"action": "decline"}) as second:
            loaded = await second.client.call_tool("js", {"code": "var app=await cua.getApp({window_id:'11'}); await app.getState();"}, request_owner={**owner, "turn_id": "second-turn"})
            assert not loaded.is_error, text(loaded)
            assert second.approvals == []
            client, query, _, _ = settings_client(monkeypatch, tmp_path, native_config())
            async with client:
                snapshot = (await client.get("/api/settings/computer-use", params=query)).json()
                result = await client.post("/api/settings/computer-use/app-approvals/remove", json={**query,
                    "provider_id": snapshot["provider_id"], "selection_id": snapshot["selection_id"], "app_id": "fixture.editor"})
                assert result.status_code == 200 and result.json()["approved_apps"] == []
            for case, request_owner in ((first, owner), (second, {**owner, "turn_id": "second-turn"})):
                revoked = await case.client.call_tool("js", {"code": "await app.getState()"}, request_owner=request_owner)
                assert revoked.is_error and "approval was revoked" in text(revoked)


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["grant_app_approval", "authorize", "lookup_app_approval"])
async def test_real_worker_cannot_upgrade_an_approved_session_target_using_private_rpc(tmp_path, monkeypatch, method):
    async with native_companion(tmp_path, monkeypatch, choice={"action": "accept", "content": {"persist": "session"}}) as case:
        code = """
var workerModule=await import('node:worker_threads');
var selectedTarget;
workerModule.parentPort.on('message', message=>{
 if(message.kind==='native_reply' && message.result?.target) selectedTarget=message.result.target;
});
var app=await cua.getApp({window_id:'11'});
var originalPost=workerModule.parentPort.postMessage;
workerModule.parentPort.postMessage=function(message,...args){
 if(message.kind==='native_request' && message.method==='get_state')
  message={...message,method:PRIVATE_METHOD,args:{target:selectedTarget,scope:'always',app_id:'fixture.editor'}};
 return originalPost.call(this,message,...args);
};
await app.getState();
""".replace("PRIVATE_METHOD", json.dumps(method))
        result = await case.client.call_tool("js", {"code": code}, request_owner={"session_id": "receiving-session",
            "conversation_id": OWNER["session_id"], "turn_id": OWNER["turn_id"]})
        assert result.is_error and "private native control method" in text(result)
        assert len(case.approvals) == 1
        assert NativeAppApprovalStore(native_approval_home()).list_apps() == []
        rows = [json.loads(line) for line in case.log.read_text().splitlines()]
        assert not [row for row in rows if row.get("method") == "grant_app_approval"]
        authorized = [row for row in rows if row.get("method") == "authorize"]
        assert len(authorized) == 1 and authorized[0]["args"]["scope"] == "session"
