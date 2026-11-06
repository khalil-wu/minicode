from __future__ import annotations

import os
from pathlib import Path

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
