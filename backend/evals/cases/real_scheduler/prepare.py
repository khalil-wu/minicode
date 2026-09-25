"""Prepare an isolated checkout with the historical scheduler defects."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
import subprocess
import sys
from zipfile import ZipFile


repo = Path(__file__).resolve().parents[4]
target = Path(sys.argv[1]).resolve()
archive = subprocess.check_output(["git", "archive", "--format=zip", "e658ffa7"], cwd=repo)
target.mkdir(parents=True)
with ZipFile(BytesIO(archive)) as package:
    package.extractall(target)

subprocess.run(["git", "init", "-b", "codex/benchmark-baseline"], cwd=target, check=True, stdout=subprocess.DEVNULL)
patch = subprocess.check_output(
    ["git", "diff", "8cac33e5^", "8cac33e5", "--", "backend/tasks/scheduler.py"],
    cwd=repo,
)
subprocess.run(["git", "apply", "--reverse", "-"], input=patch, cwd=target, check=True)
(target / "tests/test_scheduler_integrity.py").unlink()

subprocess.run(["git", "add", "."], cwd=target, check=True, stdout=subprocess.DEVNULL)
subprocess.run(
    ["git", "-c", "user.name=MiniCode Benchmark", "-c", "user.email=benchmark@local",
     "commit", "-m", "baseline: scheduler persistence and timezone failures"],
    cwd=target, check=True, stdout=subprocess.DEVNULL,
)
print(target)
