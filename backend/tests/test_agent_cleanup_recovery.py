from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import subprocess

import pytest

from backend.agent.runtime import AgentRuntime, epoch_ms
from backend.agent.swarm_store import FileSwarmStore
from backend.agent.worktree import create_agent_worktree


@pytest.fixture
def runtime_factory(tmp_path):
    runtimes = []

    def create(instance_id):
        runtime = AgentRuntime(
            metrics_file=tmp_path / "metrics.jsonl",
            swarm_store_dir=tmp_path / "swarm",
            runtime_instance_id=instance_id,
            enable_lease_heartbeat=False,
        )
        runtimes.append(runtime)
        return runtime

    yield create

    for runtime in reversed(runtimes):
        runtime.close(release_lease=True)


def _start_records(runtime, *, with_child=True):
    parent = runtime.start_run(run_id="cleanup-root", session_id="cleanup-session")
    child = (
        runtime.start_subagent(
            subagent_id="cleanup-child",
            parent_run_id=parent.run_id,
            agent_type="general-purpose",
            session_id=parent.session_id,
        )
        if with_child
        else None
    )
    return parent, child


def _mark_pending(runtime, parent, child, *, terminal=True):
    if terminal:
        if child is not None:
            runtime.store_subagent_result(
                child.subagent_id,
                status="cancelled",
                content="retained cleanup result",
                agent_path=child.agent_path,
                mailbox_epoch=child.mailbox_epoch,
            )
            child = runtime.complete_subagent(
                child.subagent_id,
                "cancelled",
                agent_path=child.agent_path,
                mailbox_epoch=child.mailbox_epoch,
            )
            assert child is not None
        parent = runtime.commit_terminal(parent.run_id, "cancelled")
    if child is not None:
        child = runtime._mark_subagent_cleanup(child.subagent_id, reason="runtime_shutdown")
        assert child is not None
    parent = replace(
        parent,
        cleanup_pending=True,
        cleanup_reason="runtime_shutdown",
        cleanup_requested_at=epoch_ms(),
        cleanup_completed_at=None,
    )
    assert runtime._swarm_store.upsert_agent_run(
        parent.to_dict(), expected_owner_token=parent.runtime_owner_token,
    ) is not None
    runtime._runs[parent.run_id] = parent
    return parent, child


def _register_worktree(runtime, child, root: Path):
    repository = root / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    subprocess.run([
        "git", "-C", str(repository), "-c", "user.name=Audit",
        "-c", "user.email=audit@example.invalid", "commit", "-q",
        "--allow-empty", "-m", "temporary cleanup fixture",
    ], check=True)
    worktree, error = create_agent_worktree(child.subagent_id, repository)
    assert worktree is not None, error
    assert runtime.register_subagent_cleanup_resource(
        child.subagent_id,
        resource_kind="worktree",
        resource_id=str(worktree.worktree_path),
        metadata={"git_root": str(repository)},
    )
    return worktree.worktree_path


def test_recovery_retains_cleanup_intents_beyond_default_history_limit(runtime_factory, tmp_path):
    original = runtime_factory("original")
    parent, child = _start_records(original)
    assert original.register_subagent_cleanup_resource(
        child.subagent_id,
        resource_kind="worktree",
        resource_id=str(tmp_path / "already-absent-worktree"),
        metadata={"git_root": str(tmp_path)},
    )
    parent, child = _mark_pending(original, parent, child)
    for index in range(1001):
        original._swarm_store.upsert_agent_run({
            **parent.to_dict(),
            "run_id": f"history-root-{index}",
            "agent_path": f"history-root-{index}",
            "completed_at": parent.completed_at + index + 1,
            "cleanup_pending": False,
        })
        original._swarm_store.upsert_subagent({
            **child.to_dict(),
            "subagent_id": f"history-child-{index}",
            "parent_run_id": f"history-root-{index}",
            "agent_path": f"history-root-{index}/child",
            "completed_at": child.completed_at + index + 1,
            "cleanup_pending": False,
            "cleanup_resources": [],
        })
    original.close(release_lease=True)

    recovered = runtime_factory("recovered")

    assert len(recovered._runs) == 1001
    assert len(recovered._subagents) == 1001
    assert recovered.get_run("history-root-0") is None
    assert recovered.get_subagent("history-child-0") is None
    restored_parent = recovered.get_run(parent.run_id)
    restored_child = recovered.get_subagent(child.subagent_id)
    assert restored_parent is not None and not restored_parent.cleanup_pending
    assert restored_child is not None and not restored_child.cleanup_pending
    assert restored_child.cleanup_resources[0]["state"] == "released"
    assert restored_child.cleanup_resources[0]["receipt"] == "already_absent"
    assert restored_parent.runtime_owner_token == recovered._runtime_owner_token
    assert restored_child.runtime_owner_token == recovered._runtime_owner_token
    result = recovered.get_subagent_snapshot(child.subagent_id)["result"]
    assert result["status"] == "cancelled"
    assert result["content"] == "retained cleanup result"
    assert not recovered._swarm_store.get_agent_run(parent.run_id)["cleanup_pending"]
    assert not recovered._swarm_store.get_subagent(child.subagent_id)["cleanup_pending"]


def test_hydration_caps_only_settled_history_and_accepts_legacy_flags(runtime_factory):
    runtime = runtime_factory("original")
    parent, child = _mark_pending(runtime, *_start_records(runtime))
    for kind, record, save, identity in (
        ("root", parent, runtime._swarm_store.upsert_agent_run, "run_id"),
        ("child", child, runtime._swarm_store.upsert_subagent, "subagent_id"),
    ):
        for index in range(2):
            save({**record.to_dict(), identity: f"pending-{kind}-{index}"})
        save({
            **record.to_dict(), identity: f"live-{kind}", "status": "running",
            "phase": "execute", "cleanup_pending": False, "completed_at": None,
        })
        for index in range(4):
            payload = {
                **record.to_dict(), identity: f"history-{kind}-{index}",
                "completed_at": record.completed_at + index + 1, "cleanup_pending": False,
            }
            if index == 3:
                del payload["cleanup_pending"]
            save(payload)

    recovered = runtime._swarm_store.recover_runtime_state(
        interrupted_at=epoch_ms(),
        summary="not applied to a live owner",
        current_instance_id=runtime._runtime_instance_id,
        current_owner_token=runtime._runtime_owner_token,
        current_process_id=runtime._runtime_process_id,
        current_process_start_identity=runtime._runtime_process_start_identity,
        active_owner_tokens={runtime._runtime_owner_token},
        hydration_limit=2,
    )

    for table, kind, identity in (
        ("runs", "root", "run_id"), ("subagents", "child", "subagent_id"),
    ):
        assert len(recovered[table]) == 6
        assert {record[identity] for record in recovered[table]} == {
            f"cleanup-{kind}", f"pending-{kind}-0", f"pending-{kind}-1",
            f"live-{kind}", f"history-{kind}-2", f"history-{kind}-3",
        }
    assert [result["subagent_id"] for result in recovered["results"]] == [child.subagent_id]


@pytest.mark.parametrize("terminal", [False, True])
@pytest.mark.parametrize("resource", ["clean-worktree", "changed-worktree", "no-resource", "root-only"])
def test_observer_skips_live_owner_cleanup_until_takeover(
    runtime_factory, tmp_path, resource, terminal,
):
    original = runtime_factory("original")
    parent, child = _start_records(original, with_child=resource != "root-only")
    worktree_path = (
        _register_worktree(original, child, tmp_path)
        if resource.endswith("worktree")
        else None
    )
    if resource == "changed-worktree":
        (worktree_path / "user-change.txt").write_text("keep user changes\n", encoding="utf-8")
    parent, child = _mark_pending(original, parent, child, terminal=terminal)
    parent_before = original._swarm_store.get_agent_run(parent.run_id)
    child_before = original._swarm_store.get_subagent(child.subagent_id) if child else None

    observer = runtime_factory("observer")

    assert observer._swarm_store.get_agent_run(parent.run_id) == parent_before
    if child is not None:
        assert observer._swarm_store.get_subagent(child.subagent_id) == child_before
    if worktree_path is not None:
        assert worktree_path.is_dir()
    assert not observer._lease_lost
    assert observer.start_run(run_id="observer-own-run").status == "running"

    original.close(release_lease=True)
    successor = runtime_factory("successor")

    assert not successor.get_run(parent.run_id).cleanup_pending
    if child is not None:
        assert not successor.get_subagent(child.subagent_id).cleanup_pending
    if resource == "changed-worktree":
        assert (worktree_path / "user-change.txt").read_text(encoding="utf-8") == "keep user changes\n"
        assert successor.get_subagent(child.subagent_id).cleanup_resources[0]["state"] == "retained"
    elif worktree_path is not None:
        assert not worktree_path.exists()


def test_lost_cleanup_takeover_does_not_touch_worktree(runtime_factory, tmp_path, monkeypatch):
    original = runtime_factory("original")
    parent, child = _start_records(original)
    worktree_path = _register_worktree(original, child, tmp_path)
    parent, child = _mark_pending(original, parent, child)
    original.close(release_lease=True)
    upsert = FileSwarmStore.upsert_subagent
    rejected = []

    def reject_takeover(store, payload, **kwargs):
        if payload["subagent_id"] == child.subagent_id and kwargs.get("allow_takeover_terminal"):
            rejected.append(payload["subagent_id"])
            return None
        return upsert(store, payload, **kwargs)

    monkeypatch.setattr(FileSwarmStore, "upsert_subagent", reject_takeover)
    observer = runtime_factory("observer")

    assert rejected == [child.subagent_id]
    assert worktree_path.is_dir()
    assert observer.get_subagent(child.subagent_id).cleanup_pending
    assert observer.get_run(parent.run_id).cleanup_pending
    assert observer._swarm_store.get_subagent(child.subagent_id)["runtime_owner_token"] == child.runtime_owner_token
    assert not observer._lease_lost
    assert observer.start_run(run_id="observer-own-run").status == "running"


def test_released_runtime_does_not_reconcile_resources(runtime_factory, tmp_path):
    original = runtime_factory("original")
    parent, child = _start_records(original)
    worktree_path = _register_worktree(original, child, tmp_path)
    parent, child = _mark_pending(original, parent, child)
    original.close(release_lease=True)

    original._reconcile_recovered_cleanup_resources()

    assert worktree_path.is_dir()
    assert original._swarm_store.get_agent_run(parent.run_id)["cleanup_pending"]
    assert original._swarm_store.get_subagent(child.subagent_id)["cleanup_pending"]
