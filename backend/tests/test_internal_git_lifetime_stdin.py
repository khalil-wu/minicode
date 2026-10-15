from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from backend.runtime_env import sanitized_git_env


@pytest.mark.parametrize("operation", ["worktree", "memory", "diff"])
def test_git_metadata_finishes_while_the_desktop_lifetime_pipe_stays_open(tmp_path, operation):
    subprocess.run(
        ["git", "init", "--quiet", str(tmp_path)],
        stdin=subprocess.DEVNULL, capture_output=True, env=sanitized_git_env(), check=True,
    )
    source = """
import sys
import asyncio
from pathlib import Path
from types import SimpleNamespace
from backend.agent.worktree import _git
from backend.memory.generation import MemoryGenerationCoordinator
from backend.diff.git_integration import _run_git

root = Path(sys.argv[1])
if sys.argv[2] == 'diff':
    assert asyncio.run(_run_git(str(root), 'cat-file', '--batch')) == ''
elif sys.argv[2] == 'worktree':
    result = _git(root, 'cat-file', '--batch')
else:
    result = MemoryGenerationCoordinator._git(SimpleNamespace(memory_root=root), 'cat-file', '--batch')
if sys.argv[2] != 'diff':
    assert result is not None and result.returncode == 0
print('actual-git-finished')
"""
    child = subprocess.Popen(
        [sys.executable, "-c", source, str(tmp_path), operation],
        cwd=Path(__file__).resolve().parents[2],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8",
    )
    try:
        child.wait(timeout=10)
        assert child.returncode == 0, child.stderr.read()
        assert child.stdout.read().strip() == "actual-git-finished"
    finally:
        child.stdin.close()
        if child.poll() is None:
            child.wait(timeout=10)
        child.stdout.close()
        child.stderr.close()
