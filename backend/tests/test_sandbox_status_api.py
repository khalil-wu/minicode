from __future__ import annotations

import copy
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from backend import main
from backend.api import routes_health
from backend.permissions import profiles
from backend.permissions.context import PermissionContext
from backend.sandbox import SandboxRunner
from backend.sandbox.runner import SandboxCapability
from backend.sandbox import windows_native
from backend.ws.handlers.session import handle_runtime_capabilities_inspect
from backend.ws.session_lifecycle import SessionLifecycle


def _status_runtime(tmp_path, monkeypatch):
    initialized = {"value": False}
    runtime = tmp_path / "codex.exe"
    runtime.write_bytes(b"fixture-runtime-not-executed")
    monkeypatch.setattr(routes_health, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(routes_health, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(routes_health, "STATE_ROOT", tmp_path / "desktop")
    monkeypatch.setattr(routes_health, "DATA_ROOT", tmp_path / "desktop" / "data")
    monkeypatch.setattr(profiles, "_native_sandbox_cache", {sys.platform: False})
    monkeypatch.setattr(windows_native, "_candidate_executables", lambda: [runtime])
    monkeypatch.setattr(windows_native, "discover_runtime", lambda: (
        SimpleNamespace(secret="fixture-must-never-be-returned") if initialized["value"] else None,
        "" if initialized["value"] else "native account state is unavailable",
    ))
    modes = []

    def capability(runner, **kwargs):
        modes.append(runner._policy.resolve(cwd=tmp_path).enforcement.value)
        return SandboxCapability(
            available=initialized["value"], backend="windows-elevated-wfp" if initialized["value"] else "unavailable",
            filesystem_isolated=initialized["value"], network_isolated=initialized["value"],
            reason="" if initialized["value"] else "native account state is unavailable",
        )

    monkeypatch.setattr(SandboxRunner, "capability", capability)
    return initialized, modes


def test_status_reprobes_managed_capability_and_replaces_negative_native_detection_after_setup(tmp_path, monkeypatch):
    initialized, modes = _status_runtime(tmp_path, monkeypatch)
    before = routes_health._build_sandbox_status_payload()
    assert before["permission_mode"] == "confirm"
    assert before["available"] is False and before["native_available"] is False
    assert before["setup_required"] is True and before["setup_supported"] is True
    assert before["sandbox_executable"] == str((tmp_path / "codex.exe").resolve())
    assert profiles.sandbox_status_for("confirm")["os"] == "app_layer"

    initialized["value"] = True
    after = routes_health._build_sandbox_status_payload()
    assert after["available"] is True and after["native_available"] is True
    assert after["setup_required"] is False
    assert after["backend"] == "windows-elevated-wfp"
    assert profiles.sandbox_status_for("confirm")["os"] == "enforced"
    assert modes and set(modes) == {"managed"}
    assert "fixture-must-never-be-returned" not in str(after)


def test_initialized_native_environment_does_not_offer_repeated_setup_for_a_policy_failure(tmp_path, monkeypatch):
    initialized, _modes = _status_runtime(tmp_path, monkeypatch)
    initialized["value"] = True
    monkeypatch.setattr(SandboxRunner, "capability", lambda *_args, **_kwargs: SandboxCapability(
        available=False, backend="unavailable", filesystem_isolated=False, network_isolated=False,
        reason="current managed policy cannot be enforced",
    ))
    snapshot = routes_health._build_sandbox_status_payload()
    assert snapshot["available"] is False and snapshot["native_available"] is True
    assert snapshot["setup_required"] is False
    assert snapshot["reason"] == "current managed policy cannot be enforced"


@pytest.mark.parametrize("configured_home", [" ./configured-home ", "~/.configured-sandbox"])
def test_setup_configuration_uses_backend_home_normalization_and_explicit_runtime(tmp_path, monkeypatch, configured_home):
    candidates = windows_native._candidate_executables
    _status_runtime(tmp_path, monkeypatch)
    monkeypatch.setattr(windows_native, "_candidate_executables", candidates)
    monkeypatch.chdir(tmp_path)
    profile = tmp_path / "profile"
    monkeypatch.setenv("HOME", str(profile))
    monkeypatch.setenv("USERPROFILE", str(profile))
    monkeypatch.setenv("MINICODE_WINDOWS_SANDBOX_HOME", configured_home)
    selected_runtime = tmp_path / "selected-runtime" / "codex.exe"
    selected_runtime.parent.mkdir()
    selected_runtime.write_bytes(b"configured-runtime-not-executed")
    monkeypatch.setenv("MINICODE_WINDOWS_SANDBOX_EXECUTABLE", " selected-runtime/codex.exe ")
    snapshot = routes_health._build_sandbox_status_payload()
    assert snapshot["sandbox_home"] == str(Path(configured_home.strip()).expanduser().resolve())
    assert snapshot["sandbox_executable"] == str(selected_runtime.resolve())
    assert snapshot["setup_supported"] is True


def test_missing_explicit_runtime_does_not_offer_the_bundled_runtime_as_a_setup_target(tmp_path, monkeypatch):
    candidates = windows_native._candidate_executables
    _status_runtime(tmp_path, monkeypatch)
    monkeypatch.setattr(windows_native, "_candidate_executables", candidates)
    resources = tmp_path / "resources"
    bundled = resources / "windows-sandbox" / "codex.exe"
    bundled.parent.mkdir(parents=True)
    bundled.write_bytes(b"bundled-runtime-not-executed")
    monkeypatch.setenv("MINICODE_APP_RESOURCES_DIR", str(resources))
    monkeypatch.setenv("MINICODE_WINDOWS_SANDBOX_EXECUTABLE", str(tmp_path / "missing-custom.exe"))
    snapshot = routes_health._build_sandbox_status_payload()
    assert snapshot["sandbox_executable"] is None
    assert snapshot["setup_supported"] is False


@pytest.mark.asyncio
async def test_sandbox_status_uses_existing_runtime_auth_and_does_not_touch_session_state(monkeypatch):
    monkeypatch.setenv("MINICODE_RUNTIME_TOKEN", "sandbox-status-token")
    snapshot = {"available": False, "backend": "unavailable", "permission_mode": "confirm"}
    calls = []

    def probe():
        calls.append(True)
        return snapshot

    monkeypatch.setattr(routes_health, "_build_sandbox_status_payload", probe)
    monkeypatch.setattr(routes_health._state, "ws_manager", SimpleNamespace(
        iter_sessions=lambda: pytest.fail("GET status must not rewrite conversation capabilities or permissions"),
    ))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://127.0.0.1") as client:
        denied = await client.get("/api/sandbox/status")
        assert denied.status_code == 401 and calls == []
        allowed = await client.get("/api/sandbox/status", headers={"x-minicode-token": "sandbox-status-token"})
    assert allowed.status_code == 200 and allowed.json() == snapshot
    assert allowed.headers["cache-control"] == "no-store"
    assert calls == [True]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["confirm", "bypass"])
async def test_explicit_setup_inspect_refreshes_real_projection_without_changing_owner_or_permission(tmp_path, monkeypatch, mode):
    permission = PermissionContext(mode=mode, workspace_root=tmp_path, conversation_id="sandbox-owner")
    owner = SimpleNamespace(
        active_conversation_id="sandbox-owner", permission_context=permission,
        skill_manager=None, is_connected=False,
    )
    lifecycle = SessionLifecycle(owner)
    owner.session_lifecycle = lifecycle
    lifecycle.workspace_root_for_conversation = lambda: tmp_path
    lifecycle._sandbox_capability_scope = lifecycle._current_sandbox_capability_scope()
    lifecycle.sandbox_capability_payload = {"backend_available": False, "backend": "unavailable"}
    messages = []
    probes = []

    def probe(workspace, captured_permission):
        assert workspace == tmp_path and captured_permission is permission
        probes.append(captured_permission)
        return {"backend_available": True if mode == "confirm" else None,
            "backend": "windows-elevated-wfp" if mode == "confirm" else "full-access", "probe_status": "ready"}

    def projection(**kwargs):
        return {"conversation_id": owner.active_conversation_id, "sandbox": copy.deepcopy(lifecycle.sandbox_capability_payload)}

    async def send(payload, **kwargs):
        messages.append(payload)

    monkeypatch.setattr("backend.ws.session_lifecycle.sandbox_capability_for_context", probe)
    owner.runtime_capabilities_payload, owner.send_payload = projection, send
    await handle_runtime_capabilities_inspect(owner, {"source": "runtime.inspect"})
    assert probes == [] and messages[-1]["sandbox"]["backend_available"] is False
    await handle_runtime_capabilities_inspect(owner, {"source": "sandbox.setup"})
    assert probes == [permission]
    assert messages[-1]["sandbox"]["backend_available"] is (True if mode == "confirm" else None)
    assert messages[-1]["sandbox"]["backend"] == ("windows-elevated-wfp" if mode == "confirm" else "full-access")
    assert owner.permission_context is permission and owner.permission_context.mode == mode
    assert owner.active_conversation_id == "sandbox-owner"
