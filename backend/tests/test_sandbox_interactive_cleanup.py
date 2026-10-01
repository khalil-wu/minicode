from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.sandbox.policy import SandboxPolicy
from backend.sandbox.runner import SandboxRunner


@pytest.mark.parametrize("interactive", [False, True])
def test_sandbox_interactive_spawn_failure_removes_prepared_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, interactive: bool
) -> None:
    runner = SandboxRunner(SandboxPolicy.bypass())
    temporary = tmp_path / "sandbox-setup"

    def wrap(*args, **kwargs) -> str:
        temporary.mkdir()
        runner._low_integrity_temp_dir = temporary
        return "launch"

    async def failed_spawn(*args, **kwargs):
        raise OSError("spawn failed")

    monkeypatch.setattr(runner, "_wrap_command", wrap)
    monkeypatch.setattr("backend.sandbox.runner.spawn_shell", failed_spawn)

    async def scenario() -> None:
        with pytest.raises(OSError, match="spawn failed"):
            if interactive:
                await runner.spawn_interactive(["server"])
            else:
                await runner.spawn_shell_interactive("server")

    asyncio.run(scenario())
    assert not temporary.exists()
    assert runner.process is None


@pytest.mark.parametrize("interactive", [False, True])
def test_sandbox_interactive_cancellation_reaps_spawned_process(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, interactive: bool
) -> None:
    runner = SandboxRunner(SandboxPolicy.bypass())
    temporary = tmp_path / "sandbox-setup"
    process = SimpleNamespace(pid=1234, returncode=None)
    ready = asyncio.Event()
    killed: list[object] = []

    def wrap(*args, **kwargs) -> str:
        temporary.mkdir()
        runner._low_integrity_temp_dir = temporary
        return "launch"

    async def spawn(*args, **kwargs):
        return process

    async def await_ready(_process) -> None:
        ready.set()
        await asyncio.Event().wait()

    async def kill_tree(_process) -> bool:
        killed.append(_process)
        runner.process = None
        runner._cleanup_sandbox_setup_state()
        return True

    monkeypatch.setattr(runner, "_wrap_command", wrap)
    monkeypatch.setattr(runner, "_await_sandbox_ready", await_ready)
    monkeypatch.setattr(runner, "_kill_tree", kill_tree)
    monkeypatch.setattr("backend.sandbox.runner.spawn_shell", spawn)

    async def scenario() -> None:
        call = asyncio.create_task(
            runner.spawn_interactive(["server"])
            if interactive
            else runner.spawn_shell_interactive("server")
        )
        await ready.wait()
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call

    asyncio.run(scenario())
    assert killed == [process]
    assert runner.process is None
    assert not temporary.exists()


def test_runtime_review_native_preparation_transfers_or_releases_real_desktop(tmp_path, monkeypatch):
    import ctypes
    import sys
    from ctypes import wintypes
    from pathlib import Path
    import pytest
    if sys.platform != "win32":
        pytest.skip("real Windows desktop ownership oracle")
    import win32api
    from backend.sandbox import windows_native as native
    from backend.sandbox.policy import SandboxPolicy

    account = win32api.GetUserName()
    runtime = native.WindowsNativeRuntime(
        tmp_path / "not-invoked.exe", tmp_path / "not-provisioned-home", account, account,
        "fixture-owner", "fixture-sid", "fixture-group",
    )
    monkeypatch.setattr(native, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(native, "discover_runtime", lambda: (runtime, ""))
    resolved = SandboxPolicy(workspace_root=tmp_path, writable_roots=()).resolve(cwd=tmp_path)
    created = []
    original_create = native.PrivateDesktop.create
    original_mkdir = Path.mkdir
    original_profile = native._permission_profile
    original_argv = native._powershell_argv

    def capture_desktop(account):
        desktop = original_create(account)
        created.append((desktop, desktop.handle))
        return desktop

    monkeypatch.setattr(native.PrivateDesktop, "create", capture_desktop)
    query = ctypes.WinDLL("user32", use_last_error=True).GetUserObjectInformationW
    query.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    query.restype = wintypes.BOOL

    def handle_is_live(handle):
        buffer = ctypes.create_unicode_buffer(256)
        required = wintypes.DWORD()
        return bool(query(wintypes.HANDLE(handle), 2, buffer, ctypes.sizeof(buffer), ctypes.byref(required)))

    def prepare():
        return native.prepare_command(
            "Write-Output harmless", cwd=tmp_path, resolved=resolved, env={},
            workspace_roots=(tmp_path,), deny_read_paths=(), deny_write_paths=(),
        )

    for stage in ("mkdir", "profile", "argv"):
        monkeypatch.setattr(Path, "mkdir", original_mkdir)
        monkeypatch.setattr(native, "_permission_profile", original_profile)
        monkeypatch.setattr(native, "_powershell_argv", original_argv)
        def fail_mkdir(path, *args, **kwargs):
            if path.parent == tmp_path / "sandbox-native-temp":
                raise PermissionError("injected owned TEMP creation failure")
            return original_mkdir(path, *args, **kwargs)
        def fail_after_temp(*args, **kwargs):
            raise ValueError("injected post-allocation preparation failure")
        if stage == "mkdir":
            monkeypatch.setattr(Path, "mkdir", fail_mkdir)
            error = PermissionError
        elif stage == "profile":
            monkeypatch.setattr(native, "_permission_profile", fail_after_temp)
            error = ValueError
        else:
            monkeypatch.setattr(native, "_powershell_argv", fail_after_temp)
            error = ValueError
        with pytest.raises(error):
            prepare()
        desktop, old_handle = created[-1]
        assert desktop.handle == 0 and not handle_is_live(old_handle)
        temp_root = tmp_path / "sandbox-native-temp"
        assert not temp_root.exists() or list(temp_root.iterdir()) == []

    monkeypatch.setattr(Path, "mkdir", original_mkdir)
    monkeypatch.setattr(native, "_permission_profile", original_profile)
    monkeypatch.setattr(native, "_powershell_argv", original_argv)
    args, desktop, private_temp = prepare()
    try:
        assert desktop.handle and handle_is_live(desktop.handle)
        assert private_temp.is_dir()
        assert args[0] == str(runtime.executable)
    finally:
        desktop.close()
        private_temp.rmdir()
