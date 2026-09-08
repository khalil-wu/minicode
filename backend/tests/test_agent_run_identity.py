from __future__ import annotations

from pathlib import Path

import pytest

from backend.agent.runtime import AgentRuntime


@pytest.mark.parametrize("stored_path", ["original/worker", ""], ids=["canonical", "legacy"])
def test_run_resume_keeps_durable_path_outside_hydrated_history(
    tmp_path: Path, stored_path: str
) -> None:
    metrics_file = tmp_path / "metrics.jsonl"
    runtime = AgentRuntime(metrics_file=metrics_file)
    runtime.start_run(run_id="original", conversation_id="conversation")
    first = runtime.start_run(
        run_id="worker",
        conversation_id="conversation",
        parent_run_id="original",
        role="subagent:general-purpose",
        task_id="worker",
        session_id="worker",
        mailbox_epoch=1,
    )
    completed = runtime.commit_terminal(first.run_id, summary="first result")
    runtime.commit_terminal("original")
    runtime._swarm_store.upsert_agent_run({**completed.to_dict(), "agent_path": stored_path})
    for index in range(1001):
        runtime._swarm_store.upsert_agent_run({
            **completed.to_dict(),
            "run_id": f"history-{index}",
            "agent_path": f"history-{index}",
        })
    runtime.close(release_lease=True)

    restored = AgentRuntime(metrics_file=metrics_file)
    assert restored.get_run("worker") is None
    restored.start_run(run_id="later-parent", conversation_id="conversation")
    resumed = restored.start_run(
        run_id="worker",
        conversation_id="conversation",
        parent_run_id="later-parent",
        role="subagent:general-purpose",
        task_id="worker",
        session_id="worker",
        mailbox_epoch=2,
    )

    assert resumed.agent_path == (stored_path or "worker")
    assert resumed.parent_run_id == "later-parent"
    assert resumed.mailbox_epoch == 2
    assert restored._swarm_store.get_agent_run("worker") == resumed.to_dict()
    registration = restored._registry.get("worker", kind="run")
    assert registration.agent_path == resumed.agent_path
    assert registration.parent_id == resumed.parent_run_id
    assert not registration.sealed
    assert restored.update_phase("worker", "execute") is not None
    final = restored.commit_terminal("worker", summary="resumed result")
    assert final.status == "completed"
    assert restored._swarm_store.get_agent_run("worker") == final.to_dict()
    restored.close(release_lease=True)


def test_matching_persisted_run_is_hydrated_without_restarting_it(tmp_path: Path) -> None:
    metrics_file = tmp_path / "metrics.jsonl"
    owner = AgentRuntime(metrics_file=metrics_file)
    observer = AgentRuntime(metrics_file=metrics_file)
    owner.start_run(
        run_id="shared-run",
        conversation_id="conversation",
        task_id="task",
        session_id="session",
    )
    expected = owner.update_phase("shared-run", "execute", summary="already executing")
    assert observer.get_run("shared-run") is None

    reused = observer.start_run(
        run_id="shared-run",
        conversation_id="conversation",
        task_id="task",
        session_id="session",
    )

    assert reused.to_dict() == expected.to_dict()
    assert observer.get_run("shared-run") is reused
    assert observer._swarm_store.get_agent_run("shared-run") == expected.to_dict()
    assert observer._registry.get("shared-run", kind="run").agent_path == reused.agent_path
    assert observer.update_phase("shared-run", "recover") is not None
    observer.close()
    owner.close(release_lease=True)


def test_persisted_run_from_another_owner_is_not_adopted(tmp_path: Path) -> None:
    metrics_file = tmp_path / "metrics.jsonl"
    observer = AgentRuntime(metrics_file=metrics_file, runtime_instance_id="observer")
    owner = AgentRuntime(metrics_file=metrics_file, runtime_instance_id="owner")
    expected = owner.start_run(run_id="owned-run", conversation_id="conversation")

    with pytest.raises(RuntimeError, match="already running"):
        observer.start_run(run_id="owned-run", conversation_id="conversation")

    assert observer.get_run("owned-run") is None
    assert observer._registry.get("owned-run", kind="run") is None
    assert observer._swarm_store.get_agent_run("owned-run") == expected.to_dict()
    assert observer._swarm_store.get_agent_run("missing-run") is None
    observer.close(release_lease=True)
    owner.close(release_lease=True)
