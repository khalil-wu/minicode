from __future__ import annotations

import os
import asyncio
import sys

import pytest

from backend.runtime_env import default_user_shell, powershell_executable
from backend.tools.command_support import command_shell_argv


@pytest.mark.skipif(os.name != "nt", reason="Windows Store activation aliases")
@pytest.mark.parametrize("relative", ["WindowsApps/pwsh.exe", "WindowsApps/Microsoft.PowerShell_8wekyb3d8bbwe/pwsh.exe"])
def test_native_shell_uses_direct_executable_and_keeps_ordinary_host_selection(tmp_path, monkeypatch, relative):
    store = tmp_path / relative
    store.parent.mkdir(parents=True)
    store.write_bytes(b"activation alias")
    direct = tmp_path / "PowerShell" / "powershell.exe"
    direct.parent.mkdir()
    direct.write_bytes(b"direct executable")
    monkeypatch.setattr("backend.runtime_env.shutil.which", lambda name: str(store if name == "pwsh" else direct) if name in {"pwsh", "powershell"} else None)
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "no-other-install"))
    assert default_user_shell()["path"] == str(store)
    assert default_user_shell(native_process=True)["path"] == str(direct)
    argv = command_shell_argv("Write-Output owned", shell="pwsh", login=False)
    assert argv[0] == str(direct)
    assert argv[1:3] == ["-NoProfile", "-Command"]


@pytest.mark.skipif(os.name != "nt", reason="Windows installed PowerShell")
def test_direct_powershell_remains_the_preferred_native_shell(tmp_path, monkeypatch):
    direct = tmp_path / "pwsh.exe"
    direct.write_bytes(b"direct executable")
    monkeypatch.setattr("backend.runtime_env.shutil.which", lambda name: str(direct) if name == "pwsh" else None)
    assert powershell_executable(native_process=True) == str(direct)


@pytest.mark.skipif(os.name != "nt", reason="Windows original Job process owner")
@pytest.mark.asyncio
async def test_native_cancellation_reaps_the_actual_selected_shell_child(tmp_path):
    import psutil

    from backend.artifact.store import ArtifactStore
    from backend.permissions.checker import PermissionChecker
    from backend.config import permission_settings_from_config
    from backend.permissions.context import ToolExecutionContext
    from backend.sandbox import SandboxPolicy
    from backend.tests.native_command_harness import configure_command_context, execute_command
    from backend.tools.command_tool import RunCommandTool

    marker = tmp_path / "owned-child.pid"
    script = f"import os,time; from pathlib import Path; Path({str(marker)!r}).write_text(str(os.getpid())); time.sleep(60)"
    command = "& '" + sys.executable.replace("'", "''") + "' -u -c '" + script.replace("'", "''") + "'"
    cancel = asyncio.Event()
    tool = RunCommandTool(ArtifactStore(storage_dir=tmp_path / "artifacts"))
    checker = PermissionChecker(permission_settings_from_config({}), tmp_path)
    context = configure_command_context(tool, ToolExecutionContext(
        permission=checker.build_context(mode="bypass"), workspace_root=tmp_path,
        cancel_event=cancel), sandbox_policy=SandboxPolicy.bypass())
    task = asyncio.create_task(execute_command(tool, {"command": command, "login": False}, context))
    child = None
    try:
        async with asyncio.timeout(15):
            while not marker.exists():
                if task.done():
                    pytest.fail(f"Native command ended before child startup: {await task}")
                await asyncio.sleep(.02)
        child = psutil.Process(int(marker.read_text()))
        cancel.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not child.is_running()
        assert context.run_context.lifecycle_cleanup_evidence()["lifecycle_cleanup_pending_count"] == 0
    finally:
        cancel.set()
        await asyncio.gather(task, return_exceptions=True)
        if child is not None and child.is_running():
            child.kill()
            await asyncio.to_thread(child.wait, 5)
