from __future__ import annotations

import asyncio
import os
import random
import sqlite3
import subprocess
import sys

import pytest

from backend.agent import runtime as runtime_module
from backend.agent.runtime import AgentRuntime
from backend.terminal.task_persistence import (
    get_process_start_time,
    load_task,
    process_identity_matches,
    save_task,
)


def _subagent_fence(runtime: AgentRuntime, subagent_id: str) -> dict[str, object]:
    record = runtime.get_subagent(subagent_id)
    assert record is not None
    return {"agent_path": record.agent_path, "mailbox_epoch": record.mailbox_epoch}


def _expire_runtime_lease(runtime: AgentRuntime) -> None:
    path = runtime._swarm_store.path  # type: ignore[attr-defined]
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE runtime_leases SET heartbeat_at = 0, expires_at = 0 "
            "WHERE owner_token = ?",
            (runtime._runtime_owner_token,),  # type: ignore[attr-defined]
        )
        connection.commit()


def test_invalid_persisted_run_status_is_never_resurrected_as_running() -> None:
    record = runtime_module._agent_run_from_dict({
        "run_id": "run-corrupt",
        "status": "mystery-live-state",
        "phase": "execute",
    })

    assert record.status == "interrupted"
    assert record.phase == "final"
    assert record.terminal_reason == "invalid_persisted_status"
    assert "invalid persisted run status" in record.error
    assert isinstance(record.completed_at, int)


def test_invalid_persisted_run_phase_is_never_resurrected_as_running() -> None:
    record = runtime_module._agent_run_from_dict({
        "run_id": "run-corrupt-phase",
        "status": "running",
        "phase": "unknown-phase",
    })

    assert record.status == "interrupted"
    assert record.phase == "final"
    assert record.terminal_reason == "invalid_persisted_phase"
    assert "invalid persisted run phase" in record.error


def test_running_run_and_subagent_are_restored_as_interrupted(tmp_path) -> None:
    metrics_file = tmp_path / "metrics.jsonl"
    runtime = AgentRuntime(
        metrics_file=metrics_file,
        runtime_instance_id="previous-process",
        runtime_process_id=2_147_483_647,
    )
    runtime.start_run(
        run_id="run-1",
        conversation_id="conversation-1",
        task_id="task-1",
        session_id="session-1",
    )
    runtime.start_subagent(
        subagent_id="subagent-1",
        parent_run_id="run-1",
        agent_type="explore",
        prompt_summary="Inspect the runtime",
        background=True,
    )

    restored = AgentRuntime(
        metrics_file=metrics_file,
        runtime_instance_id="current-process",
        runtime_process_id=os.getpid(),
    )
    snapshot = restored.list_runs(
        conversation_id="conversation-1",
        include_subagents=True,
    )

    assert snapshot["runs"][0]["status"] == "interrupted"
    assert "previous MiniCode process ended" in snapshot["runs"][0]["summary"]
    assert snapshot["subagents"][0]["status"] == "interrupted"
    assert "previous MiniCode process ended" in snapshot["subagents"][0]["result_summary"]
    assert "background_task" not in snapshot["subagents"][0]
    recovered_result = restored.get_subagent_snapshot("subagent-1")
    assert recovered_result is not None
    assert recovered_result["result_available"] is True
    assert recovered_result["result"]["status"] == "interrupted"
    assert recovered_result["result"]["error"] == "runtime_interrupted"
    assert "previous MiniCode process ended" in recovered_result["result"]["content"]
    notifications = restored.list_parent_notifications(conversation_id="conversation-1")
    assert len(notifications) == 1
    assert notifications[0]["payload"]["status"] == "interrupted"


def test_subagent_resume_config_round_trips_through_durable_record(tmp_path) -> None:
    runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl")
    parent = runtime.start_run(
        run_id="resume-config-parent",
        conversation_id="resume-config-conversation",
    )
    child = runtime.start_subagent(
        subagent_id="resume-config-child",
        parent_run_id=parent.run_id,
        agent_type="explore",
        background=True,
        resume_config={
            "provider": "custom",
            "model": "model-a",
            "isolation": "worktree",
        },
    )

    updated = runtime.update_subagent_resume_config(
        child.subagent_id,
        {
            **child.resume_config,
            "worktree_path": str(tmp_path / ".minicode" / "worktrees" / "child"),
        },
        **_subagent_fence(runtime, child.subagent_id),
    )
    assert updated is not None
    persisted = runtime.load_persisted_subagent(child.subagent_id)
    assert persisted is not None
    assert persisted.resume_config == updated.resume_config
    assert "resume_config" not in persisted.public_dict()
    assert "worktree_path" not in persisted.public_dict()

    assert runtime.update_subagent_resume_config(
        child.subagent_id,
        {"model": "stale-write"},
        agent_path=child.agent_path,
        mailbox_epoch=child.mailbox_epoch + 1,
    ) is None
    assert runtime.load_persisted_subagent(child.subagent_id).resume_config == updated.resume_config


def test_runtime_reaper_clears_cleanup_pending_from_dead_owner(tmp_path) -> None:
    metrics_file = tmp_path / "metrics.jsonl"
    previous = AgentRuntime(
        metrics_file=metrics_file,
        runtime_instance_id="cleanup-previous",
        runtime_process_id=2_147_483_647,
    )
    parent = previous.start_run(
        run_id="cleanup-reaper-parent",
        conversation_id="cleanup-reaper-conversation",
    )
    child = previous.start_subagent(
        subagent_id="cleanup-reaper-child",
        parent_run_id=parent.run_id,
        agent_type="general-purpose",
        background=True,
    )
    marked = previous._mark_subagent_cleanup(child.subagent_id, reason="shutdown")
    assert marked is not None and marked.cleanup_pending is True

    restored = AgentRuntime(
        metrics_file=metrics_file,
        runtime_instance_id="cleanup-current",
        runtime_process_id=os.getpid(),
    )
    recovered = restored.get_subagent(child.subagent_id)
    assert recovered is not None
    assert recovered.status == "interrupted"
    assert recovered.cleanup_pending is False
    assert isinstance(recovered.cleanup_completed_at, int)


def test_runtime_reaper_terminates_fenced_background_process_before_completion(tmp_path) -> None:
    metrics_file = tmp_path / "metrics.jsonl"
    session_id = "cleanup-process-session"
    previous = AgentRuntime(
        metrics_file=metrics_file,
        runtime_instance_id="cleanup-process-previous",
        runtime_process_id=2_147_483_647,
    )
    parent = previous.start_run(
        run_id="cleanup-process-parent",
        conversation_id="cleanup-process-conversation",
        session_id=session_id,
    )
    child = previous.start_subagent(
        subagent_id="cleanup-process-child",
        parent_run_id=parent.run_id,
        agent_type="general-purpose",
        background=True,
        session_id=session_id,
    )
    previous._mark_subagent_cleanup(child.subagent_id, reason="runtime_shutdown")

    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    process_start = get_process_start_time(process.pid)
    assert process_start is not None
    try:
        save_task(
            session_id=session_id,
            task_id="cleanup-owned-command",
            command="sleep",
            description="owned background command",
            cwd=str(tmp_path),
            pid=process.pid,
            process_start_time=process_start,
            started_at=0.0,
            timeout_ms=0,
            status="running",
            base_dir=tmp_path,
            conversation_id="cleanup-process-conversation",
            owner_task_id=child.subagent_id,
            parent_run_id=child.subagent_id,
            owner_pid=2_147_483_647,
            owner_start_time=1.0,
        )

        restored = AgentRuntime(
            metrics_file=metrics_file,
            runtime_instance_id="cleanup-process-current",
        )

        recovered = restored.get_subagent(child.subagent_id)
        assert recovered is not None and recovered.cleanup_pending is False
        assert process_identity_matches(process.pid, process_start) is False
        persisted = load_task(session_id, "cleanup-owned-command", base_dir=tmp_path)
        assert persisted is not None
        assert persisted.status == "interrupted"
        assert persisted.cleanup_pending is False
        assert persisted.cleanup_completed_at is not None
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


def test_runtime_reaper_keeps_pending_when_background_owner_is_still_live(tmp_path) -> None:
    metrics_file = tmp_path / "metrics.jsonl"
    session_id = "cleanup-live-owner-session"
    previous = AgentRuntime(
        metrics_file=metrics_file,
        runtime_instance_id="cleanup-live-owner-previous",
        runtime_process_id=2_147_483_647,
    )
    parent = previous.start_run(run_id="cleanup-live-owner-parent", session_id=session_id)
    child = previous.start_subagent(
        subagent_id="cleanup-live-owner-child",
        parent_run_id=parent.run_id,
        agent_type="general-purpose",
        session_id=session_id,
    )
    previous._mark_subagent_cleanup(child.subagent_id, reason="runtime_shutdown")
    save_task(
        session_id=session_id,
        task_id="cleanup-live-owner-command",
        command="pending",
        description="live owner fence",
        cwd=str(tmp_path),
        pid=None,
        started_at=0.0,
        timeout_ms=0,
        status="running",
        base_dir=tmp_path,
        owner_task_id=child.subagent_id,
        parent_run_id=child.subagent_id,
        owner_pid=os.getpid(),
        owner_start_time=get_process_start_time(os.getpid()),
    )

    restored = AgentRuntime(
        metrics_file=metrics_file,
        runtime_instance_id="cleanup-live-owner-current",
    )

    recovered = restored.get_subagent(child.subagent_id)
    assert recovered is not None
    assert recovered.cleanup_pending is True
    assert recovered.cleanup_completed_at is None


def test_runtime_reaper_keeps_pending_for_unverifiable_worktree(tmp_path) -> None:
    metrics_file = tmp_path / "metrics.jsonl"
    previous = AgentRuntime(
        metrics_file=metrics_file,
        runtime_instance_id="cleanup-worktree-previous",
        runtime_process_id=2_147_483_647,
    )
    parent = previous.start_run(run_id="cleanup-worktree-parent")
    child = previous.start_subagent(
        subagent_id="cleanup-worktree-child",
        parent_run_id=parent.run_id,
        agent_type="general-purpose",
    )
    git_root = tmp_path / "repository"
    worktree_path = git_root / ".minicode" / "worktrees" / child.subagent_id
    worktree_path.mkdir(parents=True)
    assert previous.register_subagent_cleanup_resource(
        child.subagent_id,
        resource_kind="worktree",
        resource_id=str(worktree_path),
        metadata={"git_root": str(git_root)},
    )
    previous._mark_subagent_cleanup(child.subagent_id, reason="runtime_shutdown")

    restored = AgentRuntime(
        metrics_file=metrics_file,
        runtime_instance_id="cleanup-worktree-current",
    )

    recovered = restored.get_subagent(child.subagent_id)
    assert recovered is not None
    assert recovered.cleanup_pending is True
    assert worktree_path.is_dir()


def test_reused_subagent_identity_clears_previous_result_and_wait_event(tmp_path) -> None:
    runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl")
    first_parent = runtime.start_run(run_id="parent-1", conversation_id="conversation-1")
    first = runtime.start_subagent(
        subagent_id="worker",
        parent_run_id=first_parent.run_id,
        agent_type="general-purpose",
        background=True,
    )
    runtime.complete_subagent("worker", summary="old result", **_subagent_fence(runtime, "worker"))
    runtime.store_subagent_result(
        "worker",
        status="completed",
        content="old result",
        **_subagent_fence(runtime, "worker"),
    )
    assert asyncio.run(runtime.wait_for_subagent("worker", timeout=0.01)) is True

    second_parent = runtime.start_run(run_id="parent-2", conversation_id="conversation-1")
    second = runtime.start_subagent(
        subagent_id="worker",
        parent_run_id=second_parent.run_id,
        agent_type="general-purpose",
        background=True,
    )

    snapshot = runtime.get_subagent_snapshot("worker")
    assert snapshot is not None
    assert snapshot["result_available"] is False
    assert second.mailbox_epoch == first.mailbox_epoch + 1
    assert asyncio.run(runtime.wait_for_subagent("worker", timeout=0.01)) is False

    assert runtime.store_subagent_result(
        "worker",
        status="completed",
        content="stale old result",
        agent_path=first.agent_path,
        mailbox_epoch=first.mailbox_epoch,
    ) is None
    assert runtime.complete_subagent(
        "worker",
        summary="stale old result",
        agent_path=first.agent_path,
        mailbox_epoch=first.mailbox_epoch,
    ) is None
    assert runtime.get_subagent_snapshot("worker")["status"] == "running"
    assert runtime.get_subagent_snapshot("worker")["result_available"] is False

    runtime.store_subagent_result(
        "worker",
        status="completed",
        content="new result",
        agent_path=second.agent_path,
        mailbox_epoch=second.mailbox_epoch,
    )
    runtime.complete_subagent(
        "worker",
        summary="new result",
        agent_path=second.agent_path,
        mailbox_epoch=second.mailbox_epoch,
    )
    assert asyncio.run(runtime.wait_for_subagent("worker", timeout=0.01)) is True
    assert runtime.get_subagent_snapshot("worker")["result"]["content"] == "new result"


@pytest.mark.timeout(180)
def test_incarnation_fence_survives_1000_reproducible_callback_interleavings(tmp_path) -> None:
    """Late callbacks from every previous incarnation must be harmless."""
    runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl")
    try:
        with runtime.batched_metrics():
            for seed in range(1000):
                rng = random.Random(seed)
                parent_old = runtime.start_run(
                    run_id=f"parent-old-{seed}",
                    conversation_id=f"conversation-{seed}",
                )
                old = runtime.start_subagent(
                    subagent_id=f"worker-{seed}",
                    parent_run_id=parent_old.run_id,
                    agent_type="explore",
                    background=True,
                )
                runtime.complete_subagent(
                    f"worker-{seed}",
                    summary="old completion",
                    agent_path=old.agent_path,
                    mailbox_epoch=old.mailbox_epoch,
                )
                parent_new = runtime.start_run(
                    run_id=f"parent-new-{seed}",
                    conversation_id=f"conversation-{seed}",
                )
                current = runtime.start_subagent(
                    subagent_id=f"worker-{seed}",
                    parent_run_id=parent_new.run_id,
                    agent_type="explore",
                    background=True,
                )
                stale = {"agent_path": old.agent_path, "mailbox_epoch": old.mailbox_epoch}
                fresh = {"agent_path": current.agent_path, "mailbox_epoch": current.mailbox_epoch}

                operations = [
                    lambda: runtime.store_subagent_result(
                        f"worker-{seed}",
                        status="completed",
                        content="stale result",
                        **stale,
                    ),
                    lambda: runtime.complete_subagent(
                        f"worker-{seed}",
                        summary="stale completion",
                        **stale,
                    ),
                ] * 3
                rng.shuffle(operations)
                for operation in operations:
                    assert operation() is None

                assert runtime.store_subagent_result(
                    f"worker-{seed}",
                    status="completed",
                    content="fresh result",
                    **fresh,
                ) is not None
                assert runtime.complete_subagent(
                    f"worker-{seed}",
                    summary="fresh completion",
                    **fresh,
                ) is not None

                post_terminal_operations = operations[:2]
                for operation in post_terminal_operations:
                    assert operation() is None
                snapshot = runtime.get_subagent_snapshot(f"worker-{seed}")
                assert snapshot is not None
                assert snapshot["mailbox_epoch"] == current.mailbox_epoch
                assert snapshot["result"]["content"] == "fresh result"
    finally:
        runtime.close(release_lease=True)


def test_terminal_child_notification_can_transfer_to_later_parent_turn(tmp_path) -> None:
    runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl")
    first_parent = runtime.start_run(run_id="parent-1", conversation_id="conversation-1")
    child = runtime.start_subagent(
        subagent_id="background-worker",
        parent_run_id=first_parent.run_id,
        agent_type="general-purpose",
        background=True,
        detach_from_parent=True,
    )
    runtime.complete_run(first_parent.run_id)
    runtime.complete_subagent(
        "background-worker",
        summary="finished late",
        **_subagent_fence(runtime, "background-worker"),
    )
    runtime.store_subagent_result(
        "background-worker",
        status="completed",
        content="finished late",
        **_subagent_fence(runtime, "background-worker"),
    )
    second_parent = runtime.start_run(run_id="parent-2", conversation_id="conversation-1")

    assert runtime.accepts_parent_notification(
        subagent_id="background-worker",
        mailbox_epoch=child.mailbox_epoch,
        parent_run_id=second_parent.run_id,
    ) is True
    assert runtime.accepts_parent_notification(
        subagent_id="background-worker",
        mailbox_epoch=child.mailbox_epoch + 1,
        parent_run_id=second_parent.run_id,
    ) is False


def test_second_runtime_in_same_process_does_not_interrupt_active_records(tmp_path) -> None:
    metrics_file = tmp_path / "metrics.jsonl"
    runtime = AgentRuntime(metrics_file=metrics_file)
    runtime.start_run(run_id="run-active", conversation_id="conversation-1")
    runtime.start_subagent(
        subagent_id="subagent-active",
        parent_run_id="run-active",
        agent_type="explore",
    )

    observer = AgentRuntime(metrics_file=metrics_file)
    snapshot = observer.list_runs(
        conversation_id="conversation-1",
        include_subagents=True,
    )

    assert snapshot["runs"][0]["status"] == "running"
    assert snapshot["subagents"][0]["status"] == "running"


def test_pid_reuse_does_not_keep_previous_runtime_records_alive(tmp_path) -> None:
    metrics_file = tmp_path / "metrics.jsonl"
    previous = AgentRuntime(
        metrics_file=metrics_file,
        runtime_instance_id="previous-process",
        runtime_process_id=os.getpid(),
        runtime_process_start_identity="stale-process-birth",
    )
    previous.start_run(run_id="run-pid-reused", conversation_id="conversation-1")

    restored = AgentRuntime(
        metrics_file=metrics_file,
        runtime_instance_id="current-process",
        runtime_process_id=os.getpid(),
    )

    assert restored.get_run("run-pid-reused").status == "interrupted"


def test_expired_owner_cannot_overwrite_recovered_run_via_late_callback(tmp_path) -> None:
    metrics_file = tmp_path / "metrics.jsonl"
    previous = AgentRuntime(
        metrics_file=metrics_file,
        runtime_instance_id="previous-process",
    )
    previous.start_run(run_id="run-fenced", conversation_id="conversation-1")
    _expire_runtime_lease(previous)

    restored = AgentRuntime(
        metrics_file=metrics_file,
        runtime_instance_id="current-process",
    )
    assert restored.get_run("run-fenced").status == "interrupted"

    assert previous.update_phase("run-fenced", "execute", summary="stale write") is None
    persisted = restored._swarm_store.list_agent_runs(  # type: ignore[attr-defined]
        conversation_id="conversation-1"
    )[0]
    assert persisted["status"] == "interrupted"
    assert persisted["summary"] != "stale write"


def test_expired_subagent_owner_cannot_replace_recovery_result(tmp_path) -> None:
    metrics_file = tmp_path / "metrics.jsonl"
    previous = AgentRuntime(
        metrics_file=metrics_file,
        runtime_instance_id="previous-process",
    )
    previous.start_run(run_id="run-parent", conversation_id="conversation-1")
    child = previous.start_subagent(
        subagent_id="worker-fenced",
        parent_run_id="run-parent",
        agent_type="general-purpose",
    )
    _expire_runtime_lease(previous)

    restored = AgentRuntime(
        metrics_file=metrics_file,
        runtime_instance_id="current-process",
    )
    recovered = restored.get_subagent_snapshot("worker-fenced")
    assert recovered["status"] == "interrupted"
    assert recovered["result"]["error"] == "runtime_interrupted"

    assert previous.store_subagent_result(
        "worker-fenced",
        status="completed",
        content="stale late success",
        agent_path=child.agent_path,
        mailbox_epoch=child.mailbox_epoch,
    ) is None
    persisted = restored._swarm_store.get_subagent_result("worker-fenced")  # type: ignore[attr-defined]
    assert persisted["status"] == "interrupted"
    assert persisted["content"] != "stale late success"


def test_expired_subagent_owner_cannot_forget_recovery_result(tmp_path) -> None:
    metrics_file = tmp_path / "metrics.jsonl"
    previous = AgentRuntime(
        metrics_file=metrics_file,
        runtime_instance_id="previous-process",
    )
    previous.start_subagent(
        subagent_id="worker-forget-fenced",
        parent_run_id="run-parent",
        agent_type="general-purpose",
    )
    _expire_runtime_lease(previous)

    restored = AgentRuntime(
        metrics_file=metrics_file,
        runtime_instance_id="current-process",
    )
    assert restored.get_subagent_snapshot("worker-forget-fenced")["result_available"] is True

    assert previous.forget_subagent_result("worker-forget-fenced") is False
    persisted = restored._swarm_store.get_subagent_result(  # type: ignore[attr-defined]
        "worker-forget-fenced"
    )
    assert persisted is not None
    assert persisted["status"] == "interrupted"


def test_close_stops_heartbeat_and_can_release_runtime_lease(tmp_path) -> None:
    runtime = AgentRuntime(
        metrics_file=tmp_path / "metrics.jsonl",
        runtime_instance_id="runtime-to-close",
        enable_lease_heartbeat=True,
    )
    thread = runtime._lease_thread  # type: ignore[attr-defined]
    assert thread is not None and thread.is_alive()

    assert runtime.close(release_lease=True) is True

    assert not thread.is_alive()
    assert runtime._swarm_store.list_runtime_leases() == []  # type: ignore[attr-defined]
    with pytest.raises(RuntimeError, match="lease was lost"):
        runtime.start_run(run_id="run-after-close")


def test_default_runtime_rebuilds_after_orderly_lease_release(monkeypatch, tmp_path) -> None:
    previous = AgentRuntime(
        metrics_file=tmp_path / "metrics.jsonl",
        runtime_instance_id="default-runtime-restart",
    )
    monkeypatch.setattr(runtime_module, "_DEFAULT_RUNTIME", previous)
    monkeypatch.setattr(runtime_module, "METRICS_FILE", tmp_path / "metrics.jsonl")
    monkeypatch.setattr(runtime_module, "SWARM_DIR", tmp_path / "swarm")

    assert previous.close(release_lease=True) is True
    replacement = runtime_module.default_runtime()

    assert replacement is not previous
    assert replacement.start_run(run_id="run-after-orderly-restart").status == "running"


def test_default_runtime_probe_does_not_trigger_durable_hydration(monkeypatch) -> None:
    monkeypatch.setattr(runtime_module, "_DEFAULT_RUNTIME", None)

    class UnexpectedRuntimeConstruction:
        def __init__(self, *_args, **_kwargs) -> None:
            raise AssertionError("a no-op cleanup probe must not hydrate the runtime")

    monkeypatch.setattr(runtime_module, "AgentRuntime", UnexpectedRuntimeConstruction)

    assert runtime_module.default_runtime_if_initialized() is None


def test_store_purge_refuses_another_live_runtime_lease(tmp_path) -> None:
    runtime = AgentRuntime(
        metrics_file=tmp_path / "metrics.jsonl",
        swarm_store_dir=tmp_path / "swarm",
        runtime_instance_id="live-purge-owner",
        enable_lease_heartbeat=False,
    )
    try:
        runtime.start_run(
            run_id="run-live-purge",
            conversation_id="conversation-live-purge",
        )

        with pytest.raises(RuntimeError, match="another live process"):
            runtime._swarm_store.purge_conversation(  # type: ignore[attr-defined]
                "conversation-live-purge",
                allowed_active_owner_tokens=set(),
            )

        removed = runtime.purge_conversation("conversation-live-purge")
        assert removed["run_ids"] == ["run-live-purge"]
    finally:
        runtime.close(release_lease=True)


def test_persisted_conversation_purge_does_not_load_historical_runtime(
    monkeypatch,
    tmp_path,
) -> None:
    store_dir = tmp_path / "swarm"
    previous = AgentRuntime(
        metrics_file=tmp_path / "metrics.jsonl",
        swarm_store_dir=store_dir,
        runtime_instance_id="persisted-purge-owner",
        enable_lease_heartbeat=False,
    )
    previous.start_run(
        run_id="run-persisted-purge",
        conversation_id="conversation-persisted-purge",
    )
    previous.complete_run("run-persisted-purge", summary="done")
    assert previous.close(release_lease=True) is True

    monkeypatch.setattr(runtime_module, "_DEFAULT_RUNTIME", None)
    monkeypatch.setattr(runtime_module, "SWARM_DIR", store_dir)

    removed = runtime_module.purge_persisted_conversation_runtime(
        "conversation-persisted-purge"
    )

    assert removed["run_ids"] == ["run-persisted-purge"]
    assert runtime_module.default_runtime_if_initialized() is None


def test_completed_runtime_records_and_result_survive_restart(tmp_path) -> None:
    metrics_file = tmp_path / "metrics.jsonl"
    runtime = AgentRuntime(metrics_file=metrics_file)
    runtime.start_run(
        run_id="run-complete",
        conversation_id="conversation-1",
        task_id="task-1",
    )
    runtime.update_phase("run-complete", "execute", summary="Running checks")
    runtime.complete_run("run-complete", summary="All checks passed")
    child = runtime.start_subagent(
        subagent_id="subagent-complete",
        parent_run_id="run-complete",
        agent_type="verification",
        prompt_summary="Verify the change",
        background=True,
    )
    runtime.complete_subagent(
        "subagent-complete",
        summary="Verification passed",
        tool_count=3,
        agent_path=child.agent_path,
        mailbox_epoch=child.mailbox_epoch,
    )
    runtime.store_subagent_result(
        "subagent-complete",
        status="completed",
        content="Full verification report",
        duration_ms=1250,
        iterations=2,
        tool_call_count=3,
        agent_path=child.agent_path,
        mailbox_epoch=child.mailbox_epoch,
    )

    restored = AgentRuntime(metrics_file=metrics_file)
    snapshot = restored.list_runs(
        conversation_id="conversation-1",
        include_subagents=True,
    )
    result = restored.get_subagent_snapshot("subagent-complete")

    assert snapshot["runs"][0]["status"] == "completed"
    assert snapshot["runs"][0]["summary"] == "All checks passed"
    assert snapshot["subagents"][0]["status"] == "completed"
    assert snapshot["subagents"][0]["result_summary"] == "Verification passed"
    assert snapshot["subagents"][0]["tool_count"] == 3
    assert result is not None
    assert result["result_available"] is True
    assert result["result"]["content"] == "Full verification report"
    assert result["result"]["duration_ms"] == 1250
    assert result["result"]["iterations"] == 2


def test_forgotten_subagent_result_stays_forgotten_after_restart(tmp_path) -> None:
    metrics_file = tmp_path / "metrics.jsonl"
    runtime = AgentRuntime(metrics_file=metrics_file)
    child = runtime.start_subagent(
        subagent_id="subagent-forgotten",
        parent_run_id="run-1",
        agent_type="explore",
    )
    runtime.complete_subagent(
        "subagent-forgotten",
        summary="Done",
        agent_path=child.agent_path,
        mailbox_epoch=child.mailbox_epoch,
    )
    runtime.store_subagent_result(
        "subagent-forgotten",
        status="completed",
        content="Temporary result",
        agent_path=child.agent_path,
        mailbox_epoch=child.mailbox_epoch,
    )

    assert runtime.forget_subagent_result("subagent-forgotten") is True

    restored = AgentRuntime(metrics_file=metrics_file)
    snapshot = restored.get_subagent_snapshot("subagent-forgotten")

    assert snapshot is not None
    assert snapshot["result_available"] is False
