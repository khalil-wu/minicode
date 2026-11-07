from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

import pytest

from backend.agent.runtime import AgentRuntime
from backend.agent.swarm_store import FileSwarmStore


def test_writer_reuses_full_durability_connection_across_threads(tmp_path, monkeypatch):
    with FileSwarmStore(tmp_path / "swarm") as store:
        opened = []
        connect = store._connect

        def observe(**kwargs):
            opened.append(kwargs)
            return connect(**kwargs)

        monkeypatch.setattr(store, "_connect", observe)
        with ThreadPoolExecutor(max_workers=4) as workers:
            list(workers.map(lambda index: store.append_message({
                "conversation_id": "shared", "content": str(index),
            }), range(30)))
        assert opened == []
        assert store._writer.execute("PRAGMA synchronous").fetchone()[0] == 2
        messages = store.list_messages(conversation_id="shared", limit=100)
        assert len(messages) == 30
        assert [message["seq"] for message in messages] == list(range(1, 31))
        assert len(opened) == 1
        assert Path(str(store.path) + "-wal").exists()
    assert not Path(str(store.path) + "-wal").exists()
    with closing(sqlite3.connect(store.path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 30
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


@pytest.mark.parametrize("error", [OSError("write rejected"), KeyboardInterrupt()])
def test_writer_rolls_back_interrupted_transaction_before_reuse(tmp_path, error):
    with FileSwarmStore(tmp_path / "swarm") as store:
        message = store.append_message({"conversation_id": "shared", "content": "committed"})
        with pytest.raises(type(error)):
            with store._write() as connection:
                connection.execute("DELETE FROM messages")
                raise error
        assert store.list_messages(conversation_id="shared") == [message]
        saved = store.append_message({"conversation_id": "shared", "content": "after rollback"})
        assert saved["seq"] == 2


def test_readers_observe_commits_without_waiting_for_store_writer(tmp_path):
    with FileSwarmStore(tmp_path / "swarm") as store:
        store.upsert_agent_run({"run_id": "run", "status": "running"})
        changed = threading.Event()
        commit = threading.Event()

        def update():
            with store._write() as connection:
                connection.execute("UPDATE agent_runs SET payload_json = ? WHERE run_id = ?",
                    (json.dumps({"run_id": "run", "status": "completed"}), "run"))
                changed.set()
                assert commit.wait(5)

        with ThreadPoolExecutor(max_workers=2) as workers:
            writer = workers.submit(update)
            try:
                assert changed.wait(5)
                assert workers.submit(store.get_agent_run, "run").result(timeout=2)["status"] == "running"
            finally:
                commit.set()
            writer.result(timeout=5)
        assert store.get_agent_run("run")["status"] == "completed"


def test_durable_commit_recovers_after_process_exits_without_closing_writer(tmp_path):
    root = tmp_path / "crashed"
    code = """import os, sys
from backend.agent.swarm_store import FileSwarmStore
store = FileSwarmStore(sys.argv[1])
store.upsert_agent_run({'run_id': 'committed', 'status': 'completed'})
with store._write() as connection:
    connection.execute('DELETE FROM agent_runs')
    os._exit(0)
"""
    subprocess.run([sys.executable, "-c", code, str(root)], check=True, timeout=15)
    with FileSwarmStore(root) as recovered:
        assert recovered.get_agent_run("committed") == {"run_id": "committed", "status": "completed"}
        recovered.upsert_agent_run({"run_id": "next", "status": "running"})
        assert recovered.get_agent_run("next")["status"] == "running"


def test_runtime_shutdown_releases_writer_after_lease_and_allows_new_owner(tmp_path):
    runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl", enable_lease_heartbeat=False)
    run = runtime.start_run(conversation_id="conversation")
    runtime.complete_run(run.run_id, summary="finished")
    connection = runtime._swarm_store._writer
    assert runtime.close(release_lease=True)
    assert not runtime.close(release_lease=True)
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")
    restored = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl", enable_lease_heartbeat=False)
    try:
        assert restored.get_run(run.run_id).status == "completed"
        assert restored.start_run(conversation_id="conversation").status == "running"
    finally:
        restored.close(release_lease=True)
