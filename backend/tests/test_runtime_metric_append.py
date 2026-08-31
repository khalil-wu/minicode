from __future__ import annotations

import json
import threading
from collections import deque
from pathlib import Path

from backend.agent.runtime import AgentRuntime


def test_runtime_metric_append_keeps_each_json_line_intact(tmp_path: Path) -> None:
    runtime = object.__new__(AgentRuntime)
    runtime._metrics_file = tmp_path / "metrics.jsonl"
    runtime._metric_batch_guard = threading.Lock()
    runtime._metric_batch = None
    runtime._metric_write_failures = deque(maxlen=64)

    threads = [
        threading.Thread(
            target=runtime.write_metric,
            args=("test_metric", {"index": index}),
        )
        for index in range(40)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert not any(thread.is_alive() for thread in threads)
    rows = [json.loads(line) for line in runtime._metrics_file.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 40
    assert {row["payload"] if "payload" in row else row["index"] for row in rows} == set(range(40))
