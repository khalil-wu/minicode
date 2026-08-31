from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

from backend.agent.runtime import AgentRuntime


def test_real_process_kill_recovers_running_agent_and_subagent_as_interrupted(tmp_path: Path) -> None:
    store_dir = tmp_path / "swarm"
    metrics_file = tmp_path / "metrics.jsonl"
    ready_file = tmp_path / "ready.json"
    script = "\n".join(
        [
            "import json, sys, time",
            "from pathlib import Path",
            "from backend.agent.runtime import AgentRuntime",
            "store, metrics, ready = map(Path, sys.argv[1:4])",
            "runtime = AgentRuntime(metrics_file=metrics, swarm_store_dir=store, runtime_instance_id='killed-parent', lease_ttl_ms=1000, enable_lease_heartbeat=False)",
            "parent = runtime.start_run(run_id='run-killed', conversation_id='conversation-crash')",
            "child = runtime.start_subagent(subagent_id='subagent-killed', parent_run_id=parent.run_id, agent_type='explore', background=True)",
            "ready.write_text(json.dumps({'path': child.agent_path, 'epoch': child.mailbox_epoch}), encoding='utf-8')",
            "time.sleep(60)",
        ]
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(store_dir), str(metrics_file), str(ready_file)],
        cwd=Path(__file__).parents[2],
    )
    try:
        deadline = time.monotonic() + 10
        while not ready_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert ready_file.exists(), "child runtime did not persist its running records"
        incarnation = json.loads(ready_file.read_text(encoding="utf-8"))

        process.kill()
        process.wait(timeout=10)
        time.sleep(1.2)

        restored = AgentRuntime(
            metrics_file=metrics_file,
            swarm_store_dir=store_dir,
            runtime_instance_id="restored-parent",
            lease_ttl_ms=1000,
            enable_lease_heartbeat=False,
        )
        try:
            assert restored.get_run("run-killed").status == "interrupted"
            snapshot = restored.get_subagent_snapshot("subagent-killed")
            assert snapshot is not None
            assert snapshot["status"] == "interrupted"
            assert snapshot["agent_path"] == incarnation["path"]
            assert snapshot["mailbox_epoch"] == incarnation["epoch"]
            assert snapshot["result"]["error"] == "runtime_interrupted"
        finally:
            restored.close(release_lease=True)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
