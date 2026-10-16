"""Original executor decisions survive cleanup handoff and cold reconciliation."""
from __future__ import annotations

import pytest

from backend.environments.errors import ExecutorProtocolError
from backend.terminal import task_persistence
from backend.terminal.manager import BackgroundCommandManager
from backend.tests.test_network_runner_review import owner_fixture


@pytest.mark.asyncio
async def test_original_network_audit_survives_foreground_handoff_and_cold_remote_receipt(tmp_path, monkeypatch):
    monkeypatch.setenv("MINICODE_STATE_ROOT", str(tmp_path))
    owner = owner_fixture(read_failure=True)
    owner.client.session_id = "original-session"
    audit = {"decision": "deny", "reason": "not_allowed", "host": "original.example"}
    owner.handle.network_audit.append(audit)
    with pytest.raises(ExecutorProtocolError, match="original executor reader failed") as caught:
        await owner.runner.run("original", cwd="file:///original", argv=["original"])
    manager = BackgroundCommandManager(session_id="network-recovery")
    command = await manager.retain_foreground_cleanup(runner=owner.runner, command="original",
        cwd="file:///original", conversation_id="original-conversation", task_id="original-task",
        parent_run_id="original-turn", reason=caught.value.cleanup_reason)
    expected = [{"source": "executor", "payload": audit}]
    try:
        assert command.to_dict()["network_policy_decisions"] == expected
        persisted = task_persistence.load_task("network-recovery", command.command_id)
        assert persisted.network_audit == expected
        assert persisted.executor_process_id == "original-process"
        assert persisted.executor_session_id == "original-session"
        monkeypatch.setattr(task_persistence, "process_identity_matches", lambda *_: False)
        recovered, = task_persistence.cleanup_orphaned_tasks("network-recovery")
        assert recovered.cleanup_pending and recovered.network_audit == expected
        assert recovered.environment_id == "original-environment"
        # Releasing the original proxy can append its last real decisions.
        final = {"decision": "deny", "reason": "not_allowed", "host": "final.example"}
        owner.network_lease.aclose.side_effect = lambda: owner.network_lease.network_audit.append(final)
        owner.peer.available = True
        assert await manager.cancel(command.command_id, conversation_id="original-conversation")
        assert not command.cleanup_pending
        assert command.network_audit == [*expected, {"source": "controller", "payload": final}]
        assert task_persistence.load_task("network-recovery", command.command_id) is None
    finally:
        owner.peer.available = True
        await manager.shutdown()


@pytest.mark.parametrize("reconcile", [False, True])
def test_local_executor_reconciliation_keeps_environment_and_original_network_evidence(tmp_path, monkeypatch, reconcile):
    audit = [{"source": "controller", "payload": {"decision": "deny", "host": "original.example"}}]
    task_persistence.save_task(session_id="local-network-recovery", task_id="original-task",
        command="original", description="original", cwd=tmp_path.as_uri(), pid=None,
        started_at=1, timeout_ms=0, base_dir=tmp_path, owner_pid=987654321, owner_start_time=1,
        conversation_id="original-conversation", owner_task_id="original-agent", parent_run_id="original-turn",
        environment_id="selected-local", executor_process_id="original-process", network_audit=audit)
    monkeypatch.setattr(task_persistence, "process_identity_matches", lambda *_: False)
    if reconcile:
        report = task_persistence.reconcile_owned_tasks("local-network-recovery", owner_task_ids={"original-agent"}, base_dir=tmp_path)
        assert report.completed
    else:
        recovered, = task_persistence.cleanup_orphaned_tasks("local-network-recovery", tmp_path)
        assert not recovered.cleanup_pending
    restored = task_persistence.load_task("local-network-recovery", "original-task", tmp_path)
    assert restored.environment_id == "selected-local"
    assert restored.executor_process_id == "original-process"
    assert restored.network_audit == audit
