"""Bypass matrix for the Windows low-integrity sandbox backend.

Every case runs a real command through ``SandboxRunner`` with the default
workspace policy and asserts on the filesystem afterwards, not on the command's
own claims. Skipped off Windows or when the host cannot enforce the backend.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from backend.sandbox.policy import SandboxPolicy
from backend.sandbox.runner import SandboxRunner

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows low-integrity sandbox")

# A dedicated root rather than tmp_path: the cases label the workspace, and
# pytest's own tmp tree must stay untouched by mandatory labels.
_ROOT = Path(os.environ.get("MINICODE_SANDBOX_TEST_ROOT") or r"C:\minicode-sbx-test")


def _py(script: str) -> str:
    return subprocess.list2cmdline([sys.executable, "-c", script])


@pytest.fixture()
def arena():
    root = _ROOT / f"case-{os.getpid()}-{time.time_ns() % 1_000_000}"
    ws = root / "ws"
    outside = root / "outside"
    ws.mkdir(parents=True)
    outside.mkdir()
    (ws / "hello.txt").write_text("hello", encoding="utf-8")
    (outside / "victim.txt").write_text("victim", encoding="utf-8")
    yield ws, outside
    shutil.rmtree(root, ignore_errors=True)


def _run(ws: Path, command: str, *, timeout: float = 60, allow_network: bool = False):
    policy = (
        SandboxPolicy.permissive(ws, timeout=timeout)
        if allow_network
        else SandboxPolicy.workspace_default(ws, timeout=timeout)
    )
    runner = SandboxRunner(policy)
    capability = runner.capability(cwd=ws)
    if not capability.available:
        pytest.skip(f"low-integrity backend unavailable: {capability.reason}")
    assert capability.backend == "low-integrity"
    return asyncio.run(runner.run(command, cwd=ws, host_command=command))


def test_capability_reports_filesystem_but_not_network_isolation(arena):
    ws, _ = arena
    capability = SandboxRunner(SandboxPolicy.workspace_default(ws)).capability(cwd=ws)
    if not capability.available:
        pytest.skip(capability.reason)
    assert capability.backend == "low-integrity"
    assert capability.filesystem_isolated is True
    assert capability.network_isolated is False


def test_write_inside_workspace_allowed(arena):
    ws, _ = arena
    result = _run(ws, _py(f"open(r'{ws}\\made.txt','w').write('x')"))
    assert result.exit_code == 0, result.stderr
    assert (ws / "made.txt").exists()


def test_write_outside_workspace_denied(arena):
    ws, outside = arena
    result = _run(ws, _py(f"open(r'{outside}\\evil.txt','w').write('x')"))
    assert result.exit_code != 0
    assert "PermissionError" in result.stderr
    assert not (outside / "evil.txt").exists()


def test_delete_and_modify_outside_denied(arena):
    ws, outside = arena
    victim = outside / "victim.txt"
    result = _run(ws, _py(f"import os; os.remove(r'{victim}')"))
    assert result.exit_code != 0
    assert victim.read_text(encoding="utf-8") == "victim"
    result = _run(ws, _py(f"open(r'{victim}','a').write('!')"))
    assert result.exit_code != 0
    assert victim.read_text(encoding="utf-8") == "victim"


def test_user_profile_write_denied(arena):
    ws, _ = arena
    target = Path.home() / f"minicode-sbx-escape-{os.getpid()}.txt"
    try:
        result = _run(ws, _py(f"open(r'{target}','w').write('x')"))
        assert result.exit_code != 0
        assert not target.exists()
    finally:
        target.unlink(missing_ok=True)


def test_read_outside_still_allowed(arena):
    ws, outside = arena
    result = _run(ws, _py(f"print(open(r'{outside}\\victim.txt').read())"))
    assert result.exit_code == 0, result.stderr
    assert "victim" in result.stdout


def test_junction_escape_denied(arena):
    ws, outside = arena
    cmd = f'cmd /c mklink /J "{ws}\\jump" "{outside}" && ' + _py(f"open(r'{ws}\\jump\\via.txt','w').write('x')")
    result = _run(ws, cmd)
    assert not (outside / "via.txt").exists()
    assert result.exit_code != 0


def test_child_process_inherits_restriction(arena):
    ws, outside = arena
    script = (
        "import subprocess, sys; "
        f"sys.exit(subprocess.call(['powershell','-NoProfile','-Command',\"Set-Content -Path '{outside}\\ps.txt' -Value x\"]))"
    )
    result = _run(ws, _py(script))
    assert not (outside / "ps.txt").exists()
    assert result.exit_code != 0


def test_private_temp_is_writable_and_removed(arena):
    ws, _ = arena
    result = _run(ws, _py("import os,tempfile; p=os.path.join(tempfile.gettempdir(),'t.txt'); open(p,'w').write('x'); print(tempfile.gettempdir())"))
    assert result.exit_code == 0, result.stderr
    temp_dir = Path(result.stdout.strip().splitlines()[-1])
    assert temp_dir.name.startswith("minicode-sbx-")
    assert not temp_dir.exists()


def test_owner_only_directories_created_by_the_child_stay_usable(arena):
    """CPython creates mkdtemp / mkdir(0o700) targets with an owner-only DACL.

    pytest's --basetemp and tempfile rely on this; a backend that changes the
    token's identity loses access to them, which is why the integrity-level
    design was chosen over a restricted token.
    """
    ws, _ = arena
    result = _run(ws, _py(
        "import os, tempfile; os.mkdir('d7', 0o700); open('d7/f', 'w').write('x'); "
        "d = tempfile.mkdtemp(); open(os.path.join(d, 'g'), 'w').write('y'); "
        "print(os.listdir('d7'), os.listdir(d))"
    ))
    assert result.exit_code == 0, result.stderr
    assert "['f'] ['g']" in result.stdout


def test_pytest_runs_inside_the_sandbox(arena):
    ws, _ = arena
    (ws / "test_probe.py").write_text("def test_ok():\n    assert 1 == 1\n", encoding="utf-8")
    result = _run(ws, subprocess.list2cmdline([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_probe.py"]))
    assert result.exit_code == 0, result.stderr + result.stdout
    assert "1 passed" in result.stdout


def test_host_temp_directory_is_never_labelled(arena):
    """The default policy lists %TEMP% as writable; the child must use its private TEMP instead."""
    import tempfile
    import win32security

    ws, _ = arena
    host_temp = Path(tempfile.gettempdir()).resolve()
    result = _run(ws, _py("import tempfile; print(tempfile.gettempdir())"))
    assert result.exit_code == 0, result.stderr
    assert Path(result.stdout.strip().splitlines()[-1]).resolve() != host_temp
    sd = win32security.GetNamedSecurityInfo(str(host_temp), win32security.SE_FILE_OBJECT, 0x10)
    sacl = sd.GetSecurityDescriptorSacl()
    assert sacl is None or sacl.GetAceCount() == 0 or win32security.ConvertSidToStringSid(sacl.GetAce(0)[2]) != "S-1-16-4096"


def test_protected_git_metadata_write_denied(arena):
    ws, _ = arena
    (ws / ".git").mkdir()
    (ws / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    result = _run(ws, _py(f"open(r'{ws}\\.git\\config','a').write('x')"))
    assert result.exit_code != 0
    assert (ws / ".git" / "config").read_text(encoding="utf-8") == "[core]\n"


def test_network_environment_rewritten_when_denied(arena):
    ws, _ = arena
    result = _run(ws, _py("import os; print(os.environ.get('HTTPS_PROXY'), os.environ.get('PIP_NO_INDEX'), os.environ.get('MINICODE_SANDBOX'))"))
    assert result.exit_code == 0, result.stderr
    assert "127.0.0.1:9 1 low-integrity" in result.stdout


def test_ssh_stubbed_when_network_denied(arena):
    ws, _ = arena
    result = _run(ws, "cmd /c ssh -V")
    assert result.exit_code != 0
    assert "unavailable inside the sandbox" in (result.stderr + result.stdout)


def test_network_environment_untouched_when_allowed(arena):
    ws, _ = arena
    result = _run(ws, _py("import os; print(repr(os.environ.get('MINICODE_SANDBOX_NO_NETWORK')))"), allow_network=True)
    assert result.exit_code == 0, result.stderr
    assert "None" in result.stdout


def test_timeout_kills_grandchildren(arena):
    ws, _ = arena
    pid_file = ws / "grandchild.pid"
    script = (
        "import subprocess, sys, time; "
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)']); "
        f"open(r'{pid_file}','w').write(str(p.pid)); time.sleep(120)"
    )
    result = _run(ws, _py(script), timeout=3)
    assert result.timed_out
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not pid_file.exists():
        time.sleep(0.1)
    grandchild = int(pid_file.read_text(encoding="utf-8"))
    import psutil

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and psutil.pid_exists(grandchild):
        time.sleep(0.1)
    assert not psutil.pid_exists(grandchild)
