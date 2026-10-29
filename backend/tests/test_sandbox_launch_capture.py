"""The executed process and its status use one current sandbox selection."""
from __future__ import annotations

import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from backend.sandbox import windows_native
from backend.sandbox.policy import (SandboxPolicy, PermissionProfile, FileSystemSandboxPolicy,
    FileSystemSandboxEntry, FileSystemPath, FileSystemSpecialPath, FileSystemAccessMode, NetworkSandboxPolicy)
from backend.sandbox.runner import SandboxCapability, SandboxRunner, SandboxUnavailableError
from backend.workspace.executor_local import LocalProcessBackend


def profile(workspace, *, selection="restricted-token"):
    permission = PermissionProfile.managed(FileSystemSandboxPolicy.restricted([
        FileSystemSandboxEntry(FileSystemPath.special(FileSystemSpecialPath.ROOT), FileSystemAccessMode.READ),
        FileSystemSandboxEntry(FileSystemPath.path(workspace), FileSystemAccessMode.WRITE),
    ]), network=NetworkSandboxPolicy.ENABLED)
    return SandboxPolicy(workspace_root=workspace, permission_profile=permission, windows_sandbox_selection=selection)


def runtime(path, owner):
    return windows_native.WindowsNativeRuntime(path / f"{owner}.exe", path / f"{owner}-home",
        owner + "-offline", owner + "-online", owner, owner + "-sid", owner + "-group")


def test_restricted_launch_uses_one_inspected_runtime_and_new_launch_rechecks_owner(tmp_path, monkeypatch):
    import backend.sandbox.runner as module

    monkeypatch.setattr(module, "sys", SimpleNamespace(platform="win32"))
    first, second = runtime(tmp_path, "first"), runtime(tmp_path, "second")
    discover = Mock(side_effect=[(first, ""), (second, ""), (None, "next owner unavailable")])
    monkeypatch.setattr(windows_native, "discover_restricted_runtime", discover)
    runner = SandboxRunner(profile(tmp_path))
    captured = []
    for selected in (first, second):
        wrapped, capability = runner.prepare_command("unused", cwd=tmp_path, argv=["owned-node", "--stdio"],
            on_capability=captured.append)
        assert capability.windows_runtime is selected
        assert wrapped[:2] == [str(selected.executable), "--launch-restricted-windows-sandbox"]
        request = json.loads(wrapped[2])
        assert request["codexHome"] == str(selected.home)
        assert request["commandCwd"] == str(tmp_path.resolve())
        assert request["argv"] == ["owned-node", "--stdio"]
        assert request["permissionProfile"]["type"] == "managed"
    with pytest.raises(SandboxUnavailableError, match="next owner unavailable"):
        runner.prepare_command("unused", cwd=tmp_path, argv=["owned-node"], on_capability=captured.append)
    assert discover.call_count == 3
    assert [item.windows_runtime for item in captured] == [first, second, None]
    assert not captured[-1].available


def test_elevated_wrapper_uses_current_captured_runtime_without_second_discovery(tmp_path, monkeypatch):
    import backend.sandbox.runner as module

    monkeypatch.setattr(module, "sys", SimpleNamespace(platform="win32"))
    selected = runtime(tmp_path, "elevated")
    discover = Mock(return_value=(selected, ""))
    monkeypatch.setattr(windows_native, "discover_runtime", discover)
    monkeypatch.setattr(windows_native, "STATE_ROOT", tmp_path / "state")
    desktop = SimpleNamespace(name="owned-desktop", close=Mock())
    monkeypatch.setattr(windows_native.PrivateDesktop, "create", Mock(return_value=desktop))
    runner = SandboxRunner(profile(tmp_path, selection="elevated"))
    wrapped, capability = runner.prepare_command("unused", cwd=tmp_path, argv=["owned-node"])
    assert capability.windows_runtime is selected
    assert wrapped[0] == str(selected.executable)
    assert wrapped[wrapped.index("--codex-home") + 1] == str(selected.home)
    discover.assert_called_once_with()
    assert runner._windows_private_desktop is desktop
    # No process was launched, so no sandbox-account cleanup is needed.
    runner._windows_native_temp_dir.rmdir()
    runner._windows_native_temp_dir = None
    runner._windows_private_desktop = None
    desktop.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("tty", [False, True])
@pytest.mark.parametrize("failure", ["unavailable", "spawn"])
async def test_local_failed_launch_records_actual_single_selection_and_cleans_owner(tmp_path, monkeypatch, tty, failure):
    selected = SandboxCapability(failure != "unavailable", "unavailable" if failure == "unavailable" else "full-access",
        False, False, reason="current sandbox unavailable" if failure == "unavailable" else "")
    capability = Mock(return_value=selected)
    monkeypatch.setattr(SandboxRunner, "capability", capability)
    spawn = AsyncMock(side_effect=OSError("original spawn failure"))
    monkeypatch.setattr("backend.sandbox.runner.spawn_exec", spawn)
    monkeypatch.setattr("backend.workspace.executor_local.spawn_pty", spawn)
    backend, observed = LocalProcessBackend(), []
    error = SandboxUnavailableError if failure == "unavailable" else OSError
    try:
        with pytest.raises(error, match="current sandbox unavailable" if failure == "unavailable" else "original spawn failure"):
            await backend.start([sys.executable, "-I", "-S", "-c", "print('never launched')"],
                tmp_path.as_uri(), sandbox=None, tty=tty, process_observer=observed.append)
        capability.assert_called_once_with(cwd=tmp_path)
        assert observed[0].sandbox_backend == selected.backend
        assert observed[0].sandbox_type == (None if failure == "unavailable" else "none")
        assert observed[0].closed and observed[0].released and not observed[0].accepted
        assert not backend.processes
        if failure == "unavailable":
            spawn.assert_not_called()
        else:
            spawn.assert_awaited_once()
    finally:
        await backend.close()
