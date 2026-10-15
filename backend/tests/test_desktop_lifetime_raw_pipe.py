from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import psutil
import pytest


def test_interpreter_can_exit_with_lifetime_pipe_open_without_buffered_reader_fatal():
    source = """
import sys
from backend.desktop_lifetime import bind_desktop_lifetime
bind_desktop_lifetime(sys.stdin.buffer)
print('actual-lifetime-bound', flush=True)
"""
    child = subprocess.Popen([sys.executable, "-c", source], cwd=Path(__file__).resolve().parents[2],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
        **({"start_new_session": True} if os.name != "nt" else {}))
    try:
        child.wait(timeout=10)
        stdout, stderr = child.stdout.read(), child.stderr.read()
        assert "actual-lifetime-bound" in stdout
        assert "_enter_buffered_busy" not in stderr and "Fatal Python error" not in stderr
        assert child.returncode == 0, stderr
    finally:
        child.stdin.close()
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
        child.stdout.close()
        child.stderr.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows job-object descendant lifetime")
def test_parent_eof_still_terminates_actual_backend_and_owned_descendant():
    source = """
import subprocess, sys, time
from backend.desktop_lifetime import bind_desktop_lifetime
bind_desktop_lifetime(sys.stdin.buffer)
descendant = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
print(descendant.pid, flush=True)
while True:
    time.sleep(0.05)
"""
    child = subprocess.Popen([sys.executable, "-c", source], cwd=Path(__file__).resolve().parents[2],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
    try:
        descendant_id = int(child.stdout.readline().strip())
        assert psutil.pid_exists(descendant_id)
        child.stdin.close()
        child.wait(timeout=10)
        assert not psutil.pid_exists(descendant_id)
        assert "_enter_buffered_busy" not in child.stderr.read()
    finally:
        if not child.stdin.closed:
            child.stdin.close()
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
        child.stdout.close()
        child.stderr.close()
