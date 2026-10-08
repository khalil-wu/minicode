from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from backend.memory.job_store import MemoryJobStore
from backend.preview.launcher import PreviewLaunchConfigError, _coerce_config


def test_phase2_exhausted_retry_budget_stops_until_new_input(tmp_path: Path) -> None:
    store = MemoryJobStore(tmp_path / "jobs.sqlite3")
    for now in (100, 103):
        claim = store.claim_phase2(worker_id="worker", lease_seconds=10, retry_limit=2,
                                  success_cooldown_seconds=0, now=now)
        assert claim is not None
        assert store.fail_phase2(claim, "fixture failure", retry_delay_seconds=1, now=now + 1)
    assert store.claim_phase2(worker_id="worker", lease_seconds=10, retry_limit=2,
                              success_cooldown_seconds=0, now=106) is None
    store.enqueue_phase2(now=107)
    assert store.claim_phase2(worker_id="worker", lease_seconds=10, retry_limit=2,
                              success_cooldown_seconds=0, now=108) is not None


def test_preview_runtime_args_preserve_literal_values_in_real_shell(tmp_path: Path) -> None:
    program = tmp_path / "args probe.py"
    program.write_text("import json,sys; sys.stdout.reconfigure(encoding='utf-8'); print(json.dumps(sys.argv[1:],ensure_ascii=False))", encoding="utf-8")
    expected = ["hello world", "a&b", "$PATH", "it's literal", "中文"]
    config = _coerce_config({"runtimeExecutable": sys.executable,
                             "runtimeArgs": [str(program), *expected]}, tmp_path, "fixture.launch")
    assert config is not None
    command = ["powershell.exe", "-NoProfile", "-Command", config.command] if os.name == "nt" else ["/bin/sh", "-c", config.command]
    result = subprocess.run(command, cwd=tmp_path, capture_output=True, check=True)
    assert json.loads(result.stdout.decode("utf-8")) == expected


def test_preview_declared_invalid_args_are_not_silently_ignored(tmp_path: Path) -> None:
    with pytest.raises(PreviewLaunchConfigError, match="must be a list"):
        _coerce_config({"command": "python", "args": "--version"}, tmp_path, "fixture.launch")
