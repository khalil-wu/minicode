from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest


WORKER = r'''
import contextlib,sys,time
from pathlib import Path
from backend import atomic_io
from backend.agent.execution_journal import ExecutionJournal, _process_lock_for
from backend.conversations.repository import ConversationRepository
from backend.ws.client_command_log import _process_lock as command_lock
from backend.ws.event_log import _process_lock as event_lock

kind, root, action = sys.argv[1:]
root = Path(root)
root.mkdir(parents=True, exist_ok=True)
counter = root / 'counter.txt'
atomic_io._MUTATION_LOCK_ROOT = root / 'locks'
if kind == 'mutation':
    lock = lambda: atomic_io.file_mutation_locks([counter])
elif kind == 'journal':
    journal = ExecutionJournal('owned-journal', base_dir=root)
    lock = journal._locked
elif kind == 'repository':
    repository = ConversationRepository(root)
    lock = repository._store_lock
else:
    native = (command_lock if kind == 'command' else event_lock)(counter)
    lock = lambda: native
if action == 'hold':
    with lock():
        print('held', flush=True)
        sys.stdin.readline()
else:
    for _ in range(12):
        with lock():
            value = int(counter.read_text()) if counter.exists() else 0
            time.sleep(.001)
            counter.write_text(str(value + 1))
    print('completed', flush=True)
'''


@pytest.mark.parametrize("kind", ["mutation", "journal", "repository", "command", "event"])
def test_native_storage_lock_serializes_processes_and_releases_after_owner_exit(tmp_path, kind):
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2]), "PYTHONUTF8": "1"}
    options = {"env": env, "stdin": subprocess.PIPE, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE,
        "text": True, **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {})}
    args = [sys.executable, "-c", WORKER, kind, str(tmp_path)]
    owner = subprocess.Popen([*args, "hold"], **options)
    waiting = None
    try:
        assert owner.stdout.readline().strip() == "held"
        files = list(tmp_path.rglob("*.lock"))
        assert len(files) == 1
        original = files[0].stat()
        waiting = subprocess.Popen([*args, "increment"], **options)
        with pytest.raises(subprocess.TimeoutExpired):
            waiting.communicate(timeout=.1)
        assert not (tmp_path / "counter.txt").exists()
        owner.kill()
        owner.communicate(timeout=5)
        output, error = waiting.communicate(timeout=10)
        assert waiting.returncode == 0, error
        assert output.strip() == "completed"
        assert (tmp_path / "counter.txt").read_text() == "12"
        following = subprocess.run([*args, "increment"], **options, timeout=10)
        assert following.returncode == 0, following.stderr
        assert (tmp_path / "counter.txt").read_text() == "24"
        if os.name == "nt":
            current = files[0].stat()
            assert (current.st_dev, current.st_ino) == (original.st_dev, original.st_ino)
    finally:
        for process in (owner, waiting):
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate(timeout=5)
