from __future__ import annotations

import pytest

from backend.agent.public_projection import project_public_subagent_run
from backend.agent.runtime import AgentRuntime
from backend.agent.runtime_records import _subagent_from_dict
from backend.services.subagent_service import build_subagent_status_event
from backend.ws.agent_runner import _ui_agent_state_for_event, _reset_ui_agent_state_snapshot, _reconcile_ui_agent_state_with_runtime
from backend.tools.agent_tools import TaskTool
from backend.conversations.public_projection import project_public_tool_call


def test_display_identity_survives_dispatch_status_and_session_replay_without_becoming_a_teammate(tmp_path):
    runtime = AgentRuntime(swarm_store_dir=tmp_path / "swarm", enable_lease_heartbeat=False)
    try:
        runtime.start_run(run_id="parent", conversation_id="conversation", session_id="session")
        record = runtime.start_subagent(subagent_id="ordinary", parent_run_id="parent", agent_type="explore",
            prompt_summary="Review parser", task_id="ordinary", resume_config={"task_name": "parser_review", "execution_started_at": 0})
        assert record.teammate_name == ""
        assert record.display_name == "parser_review"
        assert record.started_at > 0 and record.public_dict()["started_at"] == 0
        assert TaskTool._delegation_record(record)["started_at"] == 0
        execution_started_at = record.started_at + 25
        record = runtime.update_subagent_resume_config(record.subagent_id,
            {**record.resume_config, "execution_started_at": execution_started_at, "execution_workspace_root": str(tmp_path / "child-root")},
            agent_path=record.agent_path, mailbox_epoch=record.mailbox_epoch)
        receipt = project_public_tool_call({"id": "dispatch", "name": "task", "args": {},
            "delegations": [TaskTool._delegation_record(record)]})["delegations"][0]
        assert receipt["name"] == record.display_name
        assert receipt["agent_path"] == record.agent_path
        assert receipt["started_at"] == execution_started_at
        restored = _subagent_from_dict(record.to_dict())
        assert restored.display_name == record.display_name and restored.teammate_name == ""
        for status in ("running", "completed"):
            event = build_subagent_status_event(record.subagent_id, {**record.to_dict(), "status": status}, conversation_id="conversation")
            replay = _ui_agent_state_for_event({}, event.type, event.data)["subagents"][0]
            assert replay["displayName"] == receipt["name"]
            assert replay["agentPath"] == receipt["agent_path"]
            assert replay["startedAt"] == execution_started_at
            assert replay["acceptedAt"] == record.accepted_at
            assert replay["workspaceRoot"] == str(tmp_path / "child-root")
        record = runtime.complete_subagent(record.subagent_id, status="completed", agent_path=record.agent_path, mailbox_epoch=record.mailbox_epoch)
        event = build_subagent_status_event(record.subagent_id, record.to_dict(), conversation_id="conversation")
        replay = _ui_agent_state_for_event({}, event.type, event.data)["subagents"][0]
        assert replay["completedAt"] == record.completed_at
    finally:
        runtime.close(release_lease=True)


@pytest.mark.parametrize("evidence, expected", [
    ({"status": "running"}, 0),
    ({"status": "failed", "result_summary": "preflight failed"}, 0),
    ({"status": "cancelled"}, 0),
    ({"iteration": 1}, 123),
    ({"tool_count": 1}, 123),
    ({"status": "completed", "result_summary": "retained answer"}, 123),
])
def test_legacy_resources_are_not_start_evidence(evidence, expected):
    record = _subagent_from_dict({"subagent_id": "legacy", "started_at": 123, "accepted_at": 120, **evidence})
    projected = record.public_dict()
    assert projected["started_at"] == TaskTool._delegation_record(record)["started_at"] == expected
    assert project_public_subagent_run(projected)["started_at"] == expected


def test_legacy_child_directory_uses_its_retained_actual_worktree_without_borrowing_the_parent():
    record = _subagent_from_dict({"subagent_id": "legacy", "resume_config": {"worktree_path": "C:/fixture/worktrees/child", "cwd": "C:/fixture"}})
    assert record.workspace_root == record.public_dict()["workspace_root"] == "C:/fixture/worktrees/child"
    assert project_public_subagent_run({"resume_config": record.resume_config})["workspace_root"] == record.workspace_root
    record.resume_config["execution_workspace_root"] = ""
    assert record.workspace_root == record.public_dict()["workspace_root"] == ""


def test_relative_legacy_child_worktree_remains_unknown_instead_of_using_parent_cwd():
    record = _subagent_from_dict({"subagent_id": "legacy", "resume_config": {"worktree_path": "relative-child", "cwd": "C:/parent"}})
    assert record.workspace_root == record.public_dict()["workspace_root"] == ""
    assert project_public_subagent_run({"resume_config": record.resume_config})["workspace_root"] == ""


def test_new_parent_turn_keeps_durable_children_and_resets_only_turn_state(tmp_path):
    runtime = AgentRuntime(swarm_store_dir=tmp_path / "swarm", enable_lease_heartbeat=False)
    try:
        runtime.start_run(run_id="parent", conversation_id="owner-A", session_id="session")
        child = runtime.start_subagent(subagent_id="child", parent_run_id="parent", agent_type="explore")
        child = runtime.complete_subagent("child", status="completed", agent_path=child.agent_path, mailbox_epoch=child.mailbox_epoch)
        event = build_subagent_status_event("child", child.to_dict(), conversation_id="owner-A")
        ui = _ui_agent_state_for_event(None, event.type, event.data)
        ui.update(plan={"title": "Old turn"}, todos=[{"id": "old"}], agentProgress=[{"id": "old-progress"}])
        snapshot = {"history": ["model context"], "ui_agent_state": ui}
        reset = _reset_ui_agent_state_snapshot(snapshot)
        assert reset["history"] == snapshot["history"]
        assert reset["ui_agent_state"] == {"plan": None, "todos": [], "subagents": ui["subagents"], "agentProgress": []}
        assert snapshot["ui_agent_state"]["plan"] == {"title": "Old turn"}
        reconciled, _ = _reconcile_ui_agent_state_with_runtime(reset["ui_agent_state"], runtime=runtime, conversation_id="owner-A")
        assert reconciled["subagents"][0]["id"] == "child"
        assert reconciled["subagents"][0]["status"] == "done"
    finally:
        runtime.close(release_lease=True)


def test_restored_new_child_epoch_wins_over_previous_terminal_ui_and_old_delivery(tmp_path):
    runtime = AgentRuntime(swarm_store_dir=tmp_path / "swarm", enable_lease_heartbeat=False)
    try:
        runtime.start_run(run_id="parent", conversation_id="owner-A", session_id="session")
        first = runtime.start_subagent(subagent_id="child", parent_run_id="parent", agent_type="explore")
        first = runtime.complete_subagent("child", status="completed", agent_path=first.agent_path, mailbox_epoch=first.mailbox_epoch)
        previous_event = build_subagent_status_event("child", first.to_dict(), conversation_id="owner-A")
        previous = _ui_agent_state_for_event(None, previous_event.type, previous_event.data)
        previous["subagents"][0].update(resultAvailable=True, resultContent="Retained old result", durationMs=20,
                                        toolCallCount=3, terminationReason="success", transcriptMessages=[{"content": "Old thread turn"}])
        second = runtime.start_subagent(subagent_id="child", parent_run_id="parent", agent_type="explore")
        restored, changed = _reconcile_ui_agent_state_with_runtime(previous, runtime=runtime, conversation_id="owner-A")
        assert changed and restored["subagents"][0]["status"] == "running"
        assert restored["subagents"][0]["mailboxEpoch"] == second.mailbox_epoch
        assert restored["subagents"][0]["resultAvailable"] is False
        for field in ("resultContent", "resultError", "completedAt", "durationMs", "toolCallCount", "terminationReason"):
            assert restored["subagents"][0][field] is None
        assert restored["subagents"][0]["transcriptMessages"] == [{"content": "Old thread turn"}]
        assert _ui_agent_state_for_event(restored, previous_event.type, previous_event.data) == restored
        wrong_path = {**previous_event.data, "mailbox_epoch": second.mailbox_epoch, "agent_path": "/wrong-incarnation", "record": {}, "snapshot": {}}
        assert _ui_agent_state_for_event(restored, "subagent.done", wrong_path) == restored
        assert _ui_agent_state_for_event(restored, "subagent.done", {**wrong_path, "mailbox_epoch": second.mailbox_epoch + 1}) == restored
        second = runtime.complete_subagent("child", status="completed", agent_path=second.agent_path, mailbox_epoch=second.mailbox_epoch)
        current_event = build_subagent_status_event("child", second.to_dict(), conversation_id="owner-A")
        completed = _ui_agent_state_for_event(restored, current_event.type, current_event.data)
        assert completed["subagents"][0]["status"] == "done"
        assert completed["subagents"][0]["mailboxEpoch"] == second.mailbox_epoch
    finally:
        runtime.close(release_lease=True)


def test_legacy_record_reuses_resolved_resume_metadata_without_guessing_parent_selection():
    legacy = {
        "subagent_id": "child", "parent_run_id": "parent", "status": "completed",
        "resume_config": {"provider": "child-provider", "model": "child-model", "reasoning_effort": "high", "api_key": "never-public"},
    }
    record = _subagent_from_dict(legacy)
    projected = record.public_dict()
    assert (record.provider, record.model, record.reasoning_effort) == ("child-provider", "child-model", "high")
    assert projected["model"] == "child-model" and "resume_config" not in projected
    assert "never-public" not in str(projected)
    unknown = _subagent_from_dict({"subagent_id": "unknown"})
    assert (unknown.provider, unknown.model, unknown.reasoning_effort) == ("", "", "")
    cleared = project_public_subagent_run({**legacy, "model": "", "provider": "", "reasoning_effort": ""})
    assert (cleared["model"], cleared["provider"], cleared["reasoning_effort"]) == ("", "", "")


def test_execution_metadata_survives_persistence_live_refresh_status_and_new_incarnation(tmp_path):
    runtime = AgentRuntime(swarm_store_dir=tmp_path / "swarm")
    try:
        runtime.start_run(run_id="parent", conversation_id="conversation", session_id="session")
        record = runtime.start_subagent(
            subagent_id="child", parent_run_id="parent", agent_type="explore", session_id="session",
            provider="child-provider", model="child-model", reasoning_effort="medium",
        )
        fence = {"agent_path": record.agent_path, "mailbox_epoch": record.mailbox_epoch}
        runtime.update_subagent_lifecycle(
            "child", **fence, model="actual-next-model", provider="actual-next-provider", reasoning_effort="high",
        )
        stored = runtime._swarm_store.get_subagent("child")
        assert stored["model"] == stored["resume_config"]["model"] == "actual-next-model"
        assert stored["provider"] == stored["resume_config"]["provider"] == "actual-next-provider"
        assert stored["reasoning_effort"] == stored["resume_config"]["reasoning_effort"] == "high"
        loaded = runtime.load_persisted_subagent("child")
        assert loaded.model == "actual-next-model"
        for status in ("running", "completed"):
            event = build_subagent_status_event("child", {**stored, "status": status}, conversation_id="conversation")
            assert event.data["model"] == "actual-next-model"
            assert event.data["snapshot"]["reasoning_effort"] == "high"
            state = _ui_agent_state_for_event({}, event.type, event.data)
            assert state["subagents"][0]["model"] == "actual-next-model"
            assert state["subagents"][0]["provider"] == "actual-next-provider"
            assert state["subagents"][0]["reasoningEffort"] == "high"
        runtime.complete_subagent("child", status="completed", **fence)
        restarted = runtime.start_subagent(subagent_id="child", parent_run_id="parent", agent_type="explore")
        assert restarted.mailbox_epoch == record.mailbox_epoch + 1
        assert (restarted.model, restarted.provider, restarted.reasoning_effort) == ("", "", "")
    finally:
        runtime.close(release_lease=True)


@pytest.mark.asyncio
async def test_transcript_receipt_keeps_actual_child_execution_metadata_after_memory_eviction(tmp_path, monkeypatch):
    from backend.tests.test_ws_subagent_ownership import Session
    from backend.ws.handlers.misc import handle_subagent_transcript

    runtime = AgentRuntime(swarm_store_dir=tmp_path / "swarm")
    monkeypatch.setattr("backend.agent.runtime.default_runtime", lambda: runtime)
    try:
        runtime.start_run(run_id="parent", conversation_id="conversation-1", session_id="session-1")
        child = runtime.start_subagent(
            subagent_id="child", parent_run_id="parent", agent_type="review", session_id="session-1",
            model="actual-child-model", provider="actual-child-provider", reasoning_effort="high",
        )
        runtime.execution_journal(child.subagent_id).append("user_prompt", {"content": "Inspect the fixture"})
        runtime._subagents.clear()
        session = Session(tmp_path)
        await handle_subagent_transcript(session, {"subagent_id": "child"})
        data = session.results[-1]["data"]
        assert (data["model"], data["provider"], data["reasoning_effort"]) == ("actual-child-model", "actual-child-provider", "high")
        assert data["agent_path"] == child.agent_path and data["mailbox_epoch"] == child.mailbox_epoch
        assert data["messages"][0]["content"] == "Inspect the fixture"
        assert runtime.get_subagent("child") is None
    finally:
        runtime.close(release_lease=True)
