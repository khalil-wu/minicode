from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import threading

import pytest

from backend.agent.checkpoint import save_checkpoint
from backend.agent.context import ContextBuilder
from backend.agent.execution_journal import ExecutionJournal, ExecutionJournalCorruptionError
from backend.agent.query_recovery import prepare_query_recovery
from backend.agent.state import AgentState
from backend.conversations.projection_log import apply_value_change, value_change
from backend.tests.test_extension_execution_actions import run_child


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("MINICODE_STATE_ROOT", str(tmp_path / "state"))


@pytest.mark.asyncio
async def test_durable_entry_growth_and_checkpoint_overlap_recover_without_history_replacement(tmp_path, monkeypatch):
    snapshots = []
    def factory(api):
        async def before(event, ctx):
            for index in range(64):
                api.append_entry("state", {"index": index, "payload": "x" * 1024})
                if index == 31:
                    snapshots.append(ctx.session_manager._builder.export_snapshot())
                await ctx.session_manager.flush()
        api.on("before_agent_start", before)
    async def behavior(model, messages):
        return None
    _, builder, journal, _, state, _, _ = await run_child(tmp_path, monkeypatch, factory, behavior)
    assert state.terminal_status == "completed"
    updates = [event for event in journal.read_events() if event.payload.get("lifecycle") == "extension_state_delta"]
    assert len(updates) == 64
    assert max(len(json.dumps(event.to_dict())) for event in updates) < 4096
    assert all("context_snapshot" not in event.payload for event in updates)
    assert journal.reconstruct_context_snapshot()["extension_state"] == builder.extension_state

    checkpoint_snapshot = snapshots[0]
    original = deepcopy(checkpoint_snapshot)
    save_checkpoint(session_id="delta-resume", conversation_id="conv_child_actions",
        run_id=checkpoint_snapshot["extension_cursor"]["run_id"], user_message="continue",
        iterations=1, reply="", messages=checkpoint_snapshot["history"], context_snapshot=checkpoint_snapshot,
        tool_calls=[], active_skills=[], disabled_tools=set(), stopped_reason="timeout", last_mutation_index=0)
    restored = ContextBuilder()
    metadata = {"resume_from_checkpoint": True}
    recovery = prepare_query_recovery(session_id="delta-resume", conversation_id="conv_child_actions",
        metadata=metadata, state=AgentState(user_message="continue"), context_builder=restored,
        max_iterations_budget=3, current_run_id="new-run", execution_journal=journal)
    assert recovery.restored
    assert restored.extension_state == builder.extension_state
    assert restored.export_snapshot()["history"] == checkpoint_snapshot["history"]
    assert checkpoint_snapshot == original
    assert metadata["checkpoint_origin"]["extension_updates_replayed"] == 32


@pytest.mark.asyncio
async def test_mutation_during_flush_is_committed_in_the_following_batch(tmp_path, monkeypatch):
    started, release = threading.Event(), threading.Event()
    append = ExecutionJournal.append_once
    def held_append(self, event_type, payload, **kwargs):
        if payload.get("lifecycle") == "extension_state_delta" and payload["revision"] == 1:
            started.set()
            assert release.wait(5)
        return append(self, event_type, payload, **kwargs)
    monkeypatch.setattr(ExecutionJournal, "append_once", held_append)
    def factory(api):
        async def before(event, ctx):
            api.append_entry("first", 1)
            flushing = asyncio.create_task(ctx.session_manager.flush())
            try:
                assert await asyncio.to_thread(started.wait, 5)
                api.append_entry("second", 2)
            finally:
                release.set()
            await flushing
            await ctx.session_manager.flush()
        api.on("before_agent_start", before)
    async def behavior(model, messages):
        return None
    _, builder, journal, _, state, _, _ = await run_child(tmp_path, monkeypatch, factory, behavior)
    assert state.terminal_status == "completed"
    updates = [event.payload for event in journal.read_events() if event.payload.get("lifecycle") == "extension_state_delta"]
    assert [(item["base_revision"], item["revision"]) for item in updates] == [(0, 1), (1, 2)]
    assert journal.reconstruct_context_snapshot()["extension_state"] == builder.extension_state


def test_delta_gaps_and_missing_list_bases_are_rejected(tmp_path):
    journal = ExecutionJournal("gaps", base_dir=tmp_path)
    journal.append_lifecycle("extension_state_delta", {"run_id": "run", "base_revision": 1, "revision": 2,
        "extension_changes": [{"value": {"name": "lost predecessor"}}]})
    with pytest.raises(ExecutionJournalCorruptionError, match="earlier committed"):
        journal.replay_extension_state({}, cursor={"run_id": "run", "revision": 0})
    with pytest.raises(ValueError, match="missing its base"):
        apply_value_change([], {"items": {"2": {"value": "entry"}}, "length": 3}, in_place=True)


@pytest.mark.parametrize("before,after", [
    ({}, {"entries": [{"data": [1, 2]}]}),
    ({"labels": {"a": "old", "b": "remove"}}, {"labels": {"a": "new"}}),
    ([{"value": "prefix"}], [{"value": "prefix tail"}, None, {"new": True}]),
    ([1, 2, 3], [1]),
])
def test_private_in_place_replay_matches_materialized_projection(before, after):
    change = value_change(before, after)
    original = deepcopy(before)
    assert apply_value_change(before, change) == after
    assert apply_value_change(deepcopy(before), change, in_place=True) == after
    assert before == original


@pytest.mark.asyncio
@pytest.mark.parametrize("snapshot_before_delta", [False, True])
@pytest.mark.parametrize("checkpoint_after_delivery", [False, True])
async def test_message_handoff_survives_journal_and_checkpoint_recovery(
    tmp_path, monkeypatch, snapshot_before_delta, checkpoint_after_delivery,
):
    retained = {}
    expected = ["HOOK_RESULT_MESSAGE", "QUEUED_MESSAGE", "HOST_PENDING_MESSAGE", "HOST_USER_MESSAGE"]
    append = ExecutionJournal.append_once

    def snapshot_then_append(self, event_type, payload, **kwargs):
        if snapshot_before_delta and any(change.get("context_messages") for change in payload.get("extension_changes", [])):
            self.append_lifecycle("delivery_snapshot", {
                "run_id": payload["run_id"], "context_snapshot": retained["builder"].export_snapshot(),
            })
        return append(self, event_type, payload, **kwargs)

    monkeypatch.setattr(ExecutionJournal, "append_once", snapshot_then_append)

    def factory(api):
        async def before(event, ctx):
            actions = ctx.session_manager
            retained["builder"] = actions.query.context_builder
            retained["journal"] = actions.query.run_context.execution_journal
            api.send_message({"content": expected[1]})
            actions.query.metadata["_extension_pending_messages"] = [{"message": {"content": expected[2]}}]
            actions.query.metadata["_extension_pending_user_messages"] = [{"content": expected[3]}]
            await actions.flush()
            retained["checkpoint"] = retained["builder"].export_snapshot()
            return {"message": {"role": "custom", "content": expected[0]}}
        api.on("before_agent_start", before)

    def recover():
        snapshot = retained["checkpoint"]
        save_checkpoint(session_id="delivery-resume", conversation_id="conv_child_actions",
            run_id=snapshot["extension_cursor"]["run_id"], user_message="continue", iterations=1,
            reply="", messages=snapshot["history"], context_snapshot=snapshot, tool_calls=[],
            active_skills=[], disabled_tools=set(), stopped_reason="timeout", last_mutation_index=0)
        restored = ContextBuilder()
        metadata = {"resume_from_checkpoint": True}
        result = prepare_query_recovery(session_id="delivery-resume", conversation_id="conv_child_actions",
            metadata=metadata, state=AgentState(user_message="continue"), context_builder=restored,
            max_iterations_budget=3, current_run_id="new-run", execution_journal=retained["journal"])
        assert result.restored
        delivered = [message for message in restored._history if message.content.startswith("User-configured hook feedback:")]
        assert [message.content.split("\n", 1)[1] for message in delivered] == expected
        assert all(not message.is_user_input for message in delivered)
        assert [message.timestamp_ms for message in delivered] == retained["timestamps"]
        assert "pending_messages" not in restored.extension_state
        assert metadata["checkpoint_origin"]["extension_messages_replayed"] == (0 if checkpoint_after_delivery else 4)

    async def behavior(model, messages):
        builder = retained["builder"]
        delivered = [message for message in builder._history if message.content.startswith("User-configured hook feedback:")]
        retained["timestamps"] = [message.timestamp_ms for message in delivered]
        snapshot = retained["journal"].reconstruct_context_snapshot()
        recovered = [message for message in snapshot["history"] if message["content"].startswith("User-configured hook feedback:")]
        assert [message["content"].split("\n", 1)[1] for message in recovered] == expected
        assert [message["timestamp_ms"] for message in recovered] == retained["timestamps"]
        assert "pending_messages" not in snapshot["extension_state"]
        if checkpoint_after_delivery:
            retained["checkpoint"] = builder.export_snapshot()
        recover()

    _, _, _, _, state, events, _ = await run_child(tmp_path, monkeypatch, factory, behavior)
    assert state.terminal_status == "completed", (state.stopped_reason, events)
    # A later terminal snapshot must not cause checkpoint replay to skip the
    # deliveries that were absent from the checkpoint's model history.
    recover()


@pytest.mark.asyncio
async def test_host_entry_migration_keeps_large_private_state_out_of_the_next_delta(tmp_path, monkeypatch):
    def factory(api):
        async def before(event, ctx):
            for index in range(64):
                entry_id = api.append_entry("existing", {"index": index, "value": "x" * 1024})
            await ctx.session_manager.flush()
            ctx.session_manager.query.metadata["_extension_entries"] = [{"custom_type": "migrated", "data": 65}]
            ctx.session_manager.query.metadata["_extension_labels"] = [{"entry_id": entry_id, "label": "bookmark"}]
        api.on("before_agent_start", before)

    async def behavior(model, messages):
        return None

    _, builder, journal, _, state, _, _ = await run_child(tmp_path, monkeypatch, factory, behavior)
    assert state.terminal_status == "completed"
    updates = [event for event in journal.read_events() if event.payload.get("lifecycle") == "extension_state_delta"]
    assert len(updates) == 2
    assert len(json.dumps(updates[-1].to_dict())) < 2048
    assert len(builder.extension_state["entries"]) == 65
    assert builder.extension_state["entries"][-1]["custom_type"] == "migrated"
    assert journal.reconstruct_context_snapshot()["extension_state"] == builder.extension_state


@pytest.mark.asyncio
async def test_cancelled_flush_keeps_committed_revision_and_later_updates_replayable(tmp_path, monkeypatch):
    started, release = threading.Event(), threading.Event()
    append = ExecutionJournal.append_once

    def held_append(self, event_type, payload, **kwargs):
        if payload.get("lifecycle") == "extension_state_delta" and payload["revision"] == 1:
            started.set()
            assert release.wait(5)
        return append(self, event_type, payload, **kwargs)

    monkeypatch.setattr(ExecutionJournal, "append_once", held_append)

    def factory(api):
        async def before(event, ctx):
            api.append_entry("first", 1)
            flushing = asyncio.create_task(ctx.session_manager.flush())
            try:
                assert await asyncio.to_thread(started.wait, 5)
                flushing.cancel()
                api.append_entry("second", 2)
            finally:
                release.set()
            with pytest.raises(asyncio.CancelledError):
                await flushing
            await ctx.session_manager.flush()
        api.on("before_agent_start", before)

    async def behavior(model, messages):
        journal = ExecutionJournal("child-actions", base_dir=tmp_path / "child-journal")
        assert [entry["custom_type"] for entry in journal.reconstruct_context_snapshot()["extension_state"]["entries"]] == ["first", "second"]

    _, builder, journal, _, state, _, _ = await run_child(tmp_path, monkeypatch, factory, behavior)
    assert state.terminal_status == "completed"
    assert journal.reconstruct_context_snapshot()["extension_state"] == builder.extension_state
