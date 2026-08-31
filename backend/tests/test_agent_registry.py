from dataclasses import dataclass

import pytest

from backend.agent.agent_registry import AgentRegistry
from backend.agent.runtime import AgentRuntime


@dataclass
class Record:
    subagent_id: str
    agent_path: str
    parent_run_id: str = "parent"
    mailbox_epoch: int = 1
    status: str = "running"


def test_registry_keeps_path_immutable_and_accepts_current_epoch() -> None:
    registry = AgentRegistry()
    registry.register(Record("child", "run/child"), kind="subagent")

    assert registry.accepts_mailbox(
        subagent_id="child", mailbox_epoch=1, parent_run_id="parent"
    )
    assert not registry.accepts_mailbox(
        subagent_id="child", mailbox_epoch=0, parent_run_id="parent"
    )
    assert not registry.accepts_mailbox(
        subagent_id="child", mailbox_epoch=1, parent_run_id="other"
    )

    with pytest.raises(ValueError, match="immutable"):
        registry.register(Record("child", "other/child", mailbox_epoch=2), kind="subagent")


def test_registry_seal_rejects_reopen_without_new_epoch() -> None:
    registry = AgentRegistry()
    registry.register(Record("child", "run/child"), kind="subagent")
    registry.seal("child", kind="subagent")
    assert not registry.accepts_update(
        agent_id="child", kind="subagent", agent_path="run/child", mailbox_epoch=1
    )

    with pytest.raises(ValueError, match="sealed"):
        registry.register(Record("child", "run/child", mailbox_epoch=1), kind="subagent")

    next_record = Record("child", "run/child", mailbox_epoch=2)
    registry.register(next_record, kind="subagent")
    assert registry.get("child", kind="subagent").mailbox_epoch == 2


def test_runtime_terminal_sealing_rejects_late_phase_update(tmp_path) -> None:
    runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl")
    record = runtime.start_run(run_id="run-sealed")
    runtime.complete_run(record.run_id, summary="done")

    assert runtime.update_phase(record.run_id, "recover", summary="late") is None
    assert runtime.get_run(record.run_id).phase == "final"
