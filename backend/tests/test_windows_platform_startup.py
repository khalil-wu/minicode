"""Windows task preparation must not depend on the WMI service."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.skipif(sys.platform != "win32", reason="exercises the native Windows runtime and bundled engine")
def test_prompt_terminal_and_real_policy_engine_without_wmi(tmp_path: Path):
    root = Path(__file__).resolve().parents[2]
    script = r'''
import platform

def forbidden_query(*args, **kwargs):
    raise AssertionError("Windows task preparation requested WMI-backed platform data")

for name in ("system", "machine", "release", "version", "uname"):
    setattr(platform, name, forbidden_query)

from backend.agent.prompting import build_static_environment_info
from backend.permissions.native_execpolicy import NativeExecPolicyRuntime, SOURCE_COMMIT
from backend.terminal.session import TerminalSession

prompt = build_static_environment_info()
assert "Windows " in prompt and "platform win32" in prompt
assert "command syntax is PowerShell" in prompt
assert TerminalSession("native-platform", conversation_id="owned")._is_windows
identity = NativeExecPolicyRuntime().identity()
assert SOURCE_COMMIT in str(identity), identity
'''
    environment = {**os.environ, "PYTHONPATH": str(root),
        "MINICODE_USER_DATA_DIR": str(tmp_path), "MINICODE_STATE_ROOT": str(tmp_path),
        "MINICODE_CONFIG_DIR": str(tmp_path / "config")}
    completed = subprocess.run([sys.executable, "-X", "utf8", "-c", script],
        cwd=root, env=environment, capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert completed.returncode == 0, completed.stdout + completed.stderr
