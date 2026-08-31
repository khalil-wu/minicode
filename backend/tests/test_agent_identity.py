from __future__ import annotations

from pathlib import Path

from backend.agent.agent_identity import AgentPath, MailboxEpoch
from backend.agent.runtime import AgentRuntime


def test_agent_path_is_stable_and_composable() -> None:
    root = AgentPath.main("run-root")
    child = root.child("researcher")
    assert child.value == "run-root/researcher"
    assert AgentPath.parse(child.value) == child
    assert child.child("validator").value == "run-root/researcher/validator"


def test_mailbox_epoch_advances_for_reused_subagent_identity(tmp_path: Path) -> None:
    runtime = AgentRuntime(
        metrics_file=tmp_path / "metrics.jsonl",
        swarm_store_dir=tmp_path / "swarm",
    )
    parent = runtime.start_run(conversation_id="conv", run_id="run-parent")
    first = runtime.start_subagent(
        subagent_id="researcher",
        parent_run_id=parent.run_id,
        agent_type="research",
    )
    runtime.complete_subagent(
        "researcher",
        status="completed",
        summary="first",
        agent_path=first.agent_path,
        mailbox_epoch=first.mailbox_epoch,
    )
    second = runtime.start_subagent(
        subagent_id="researcher",
        parent_run_id=parent.run_id,
        agent_type="research",
    )
    assert first.agent_path == "run-parent/researcher"
    assert second.agent_path == first.agent_path
    assert second.mailbox_epoch == first.mailbox_epoch + 1


def test_resumed_subagent_keeps_path_across_parent_turn_handoff(tmp_path: Path) -> None:
    runtime = AgentRuntime(
        metrics_file=tmp_path / "metrics.jsonl",
        swarm_store_dir=tmp_path / "swarm",
    )
    first_parent = runtime.start_run(conversation_id="conv", run_id="run-parent-1")
    first = runtime.start_subagent(
        subagent_id="researcher",
        parent_run_id=first_parent.run_id,
        agent_type="research",
    )
    runtime.complete_subagent(
        "researcher",
        status="completed",
        summary="first",
        agent_path=first.agent_path,
        mailbox_epoch=first.mailbox_epoch,
    )
    runtime.complete_run(first_parent.run_id)
    second_parent = runtime.start_run(conversation_id="conv", run_id="run-parent-2")

    resumed = runtime.start_subagent(
        subagent_id="researcher",
        parent_run_id=second_parent.run_id,
        agent_type="research",
    )

    assert resumed.agent_path == first.agent_path
    assert resumed.parent_run_id == second_parent.run_id
    assert resumed.mailbox_epoch == first.mailbox_epoch + 1


def test_running_subagent_identity_cannot_be_reopened(tmp_path: Path) -> None:
    runtime = AgentRuntime(
        metrics_file=tmp_path / "metrics.jsonl",
        swarm_store_dir=tmp_path / "swarm",
    )
    parent = runtime.start_run(conversation_id="conv", run_id="run-parent")
    runtime.start_subagent(
        subagent_id="researcher",
        parent_run_id=parent.run_id,
        agent_type="research",
    )

    try:
        runtime.start_subagent(
            subagent_id="researcher",
            parent_run_id=parent.run_id,
            agent_type="research",
        )
    except RuntimeError as exc:
        assert "already running" in str(exc)
    else:  # pragma: no cover - explicit lifecycle invariant
        raise AssertionError("running subagent identity was reopened")


def test_subagent_terminal_updates_require_complete_incarnation_fence(tmp_path: Path) -> None:
    runtime = AgentRuntime(
        metrics_file=tmp_path / "metrics.jsonl",
        swarm_store_dir=tmp_path / "swarm",
    )
    parent = runtime.start_run(conversation_id="conv", run_id="run-parent")
    record = runtime.start_subagent(
        subagent_id="researcher",
        parent_run_id=parent.run_id,
        agent_type="research",
    )

    assert runtime.complete_subagent("researcher", status="completed") is None
    assert runtime.store_subagent_result(
        "researcher",
        status="completed",
        content="unfenced",
    ) is None
    assert runtime.get_subagent("researcher").status == "running"

    assert runtime.complete_subagent(
        "researcher",
        status="completed",
        agent_path=record.agent_path,
        mailbox_epoch=record.mailbox_epoch,
    ) is not None
