from __future__ import annotations

import pytest

from backend.agent.public_projection import project_public_subagent_run
from backend.agent.runtime import AgentRuntime
from backend.agent.runtime_records import _subagent_from_dict
from backend.services.subagent_service import build_subagent_status_event
from backend.ws.agent_runner import _ui_agent_state_for_event


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
