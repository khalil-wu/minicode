"""Prepare an isolated MiniCode checkout with the historical compaction bug."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
import subprocess
import sys
from zipfile import ZipFile


repo = Path(__file__).resolve().parents[4]
target = Path(sys.argv[1]).resolve()
archive = subprocess.check_output(
    ["git", "archive", "--format=zip", "e658ffa7"], cwd=repo,
)
target.mkdir(parents=True)
with ZipFile(BytesIO(archive)) as package:
    package.extractall(target)

for relative in (
    "backend/agent/context.py",
    "backend/agent/turn_iteration_admission.py",
    "backend/services/context_budget.py",
):
    before = subprocess.check_output(
        ["git", "show", f"2845fe14^:{relative}"], cwd=repo,
    )
    (target / relative).write_bytes(before)
(target / "docs/long-session-compaction.md").unlink()

subprocess.run(["git", "init", "-b", "codex/benchmark-baseline"], cwd=target, check=True, stdout=subprocess.DEVNULL)
subprocess.run(["git", "add", "."], cwd=target, check=True, stdout=subprocess.DEVNULL)
subprocess.run(
    ["git", "-c", "user.name=MiniCode Benchmark", "-c", "user.email=benchmark@local",
     "commit", "-m", "baseline: long-session compaction failure"],
    cwd=target, check=True, stdout=subprocess.DEVNULL,
)
print(target)
