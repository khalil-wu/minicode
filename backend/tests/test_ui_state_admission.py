from __future__ import annotations

import asyncio
from copy import deepcopy
import threading
from types import SimpleNamespace

import pytest

from backend.agent.query_engine import QueryEngine
from backend.conversations.repository import ConversationRepository
from backend.tests.test_user_input_presentation_chain import session_for


def previous_ui():
    return {"plan": None, "todos": [{"id": "previous", "content": "Previous completed work", "status": "completed"}],
        "subagents": [{"id": "existing-child", "status": "done", "resultContent": "Original child result"}],
        "agentProgress": []}


def metadata():
    return {"user_message_id": "current-input", "assistant_message_id": "current-answer"}


async def complete(session):
    await asyncio.gather(*tuple(session.execution_cleanup_tasks))
    await session.event_outbox.drain_delivery()
    await session.event_outbox.drain_persistence()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["admit", "cancel", "fail"])
async def test_previous_process_state_changes_only_with_admitted_input(tmp_path, monkeypatch, outcome):
    session, owner, adapter = session_for(tmp_path, monkeypatch)
    repo = session.conversation_repo
    repo.save_context_snapshot(owner.id, {"ui_agent_state": previous_ui(), "_ui_agent_state_revision": 7})
    entered, release = asyncio.Event(), asyncio.Event()

    async def prepare(_turn):
        entered.set()
        await release.wait()
        if outcome == "fail":
            raise OSError("startup preparation failed")

    monkeypatch.setattr(QueryEngine, "_publish_system_prompt", staticmethod(prepare))
    task = asyncio.create_task(session._run_agent("Current input", conversation_id=owner.id, metadata=metadata()))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        fresh = ConversationRepository(repo.storage_root).get_conversation(owner.id)
        assert fresh.context_snapshot["ui_agent_state"] == previous_ui()
        assert fresh.transcript == [] and not adapter.requests
        assert session.ui_agent_state_store.cache[owner.id][1]["todos"] == []
        if outcome == "cancel":
            task.cancel()
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await complete(session)
        fresh = ConversationRepository(repo.storage_root).get_conversation(owner.id)
        if outcome == "admit":
            assert len(adapter.requests) == 1
            assert fresh.context_snapshot["ui_agent_state"]["todos"] == []
            assert any(row["id"] == "current-input" for row in fresh.transcript)
            assert fresh.context_snapshot["_ui_agent_state_revision"] > 7
        else:
            assert not adapter.requests
            assert fresh.context_snapshot["ui_agent_state"] == previous_ui()
            assert not any(row["id"] == "current-input" for row in fresh.transcript)
            assert owner.id not in session.ui_agent_state_store.cache
        assert fresh.context_snapshot["ui_agent_state"]["subagents"] == previous_ui()["subagents"]
        assert owner.id not in session.ui_agent_state_store.startup_reset_fields
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await session.session_lifecycle.shutdown(reason="ui-reset-admission-boundary")


@pytest.mark.asyncio
@pytest.mark.parametrize("update", ["startup", "startup_same", "peer", "reconcile"])
async def test_actual_updates_during_startup_survive_the_combined_admission(tmp_path, monkeypatch, update):
    session, owner, adapter = session_for(tmp_path, monkeypatch)
    repo = session.conversation_repo
    repo.save_context_snapshot(owner.id, {"ui_agent_state": previous_ui(), "_ui_agent_state_revision": 7})

    async def prepare(_turn):
        if update.startswith("startup"):
            await _turn.emit_event("task.update", {"conversation_id": owner.id,
                "message_id": "current-answer", "todos": (previous_ui()["todos"] if update == "startup_same" else
                    [{"id": "new-task", "content": "Actual startup task", "status": "in_progress"}])})
        elif update == "peer":
            latest = previous_ui()
            latest["subagents"].append({"id": "new-child", "status": "done", "resultContent": "Peer completed"})
            latest["plan"] = {"steps": [{"step": "Peer update", "status": "in_progress"}]}
            ConversationRepository(repo.storage_root).patch_context_snapshot(owner.id,
                {"ui_agent_state": latest}, revision=9, revision_key="_ui_agent_state_revision")
        else:
            runtime = SimpleNamespace(
                list_runs=lambda **_: {"subagents": [{"subagent_id": "existing-child"}]},
                get_run=lambda _: SimpleNamespace(conversation_id=owner.id),
                get_subagent_snapshot=lambda *_, **__: {"subagent_id": "existing-child", "parent_run_id": "parent",
                    "agent_type": "explore", "status": "completed", "summary": "New verified child result",
                    "result": {"status": "completed", "content": "New verified child result"}},
            )
            with monkeypatch.context() as scope:
                scope.setattr("backend.ws.agent_runner.default_runtime", lambda: runtime)
                await session.reconcile_persisted_ui_agent_state(owner.id)

    monkeypatch.setattr(QueryEngine, "_publish_system_prompt", staticmethod(prepare))
    try:
        await asyncio.create_task(session._run_agent("Current input", conversation_id=owner.id, metadata=metadata()))
        await complete(session)
        assert len(adapter.requests) == 1
        state = ConversationRepository(repo.storage_root).get_conversation(owner.id).context_snapshot["ui_agent_state"]
        if update.startswith("startup"):
            assert [item["id"] for item in state["todos"]] == (["previous"] if update == "startup_same" else ["new-task"])
        elif update == "peer":
            assert state["todos"] == []
            assert state["plan"]["steps"][0]["step"] == "Peer update"
            assert state["subagents"][-1]["id"] == "new-child"
        else:
            assert state["todos"] == []
            assert state["subagents"][0]["resultContent"] == "New verified child result"
        if update != "reconcile":
            assert state["subagents"][0] == previous_ui()["subagents"][0]
    finally:
        await session.session_lifecycle.shutdown(reason="ui-reset-real-updates")


@pytest.mark.asyncio
async def test_event_arriving_during_admission_write_is_not_cleared_by_its_older_receipt(tmp_path, monkeypatch):
    session, owner, adapter = session_for(tmp_path, monkeypatch)
    repo = session.conversation_repo
    repo.save_context_snapshot(owner.id, {"ui_agent_state": previous_ui()})
    entered, release = threading.Event(), threading.Event()
    commit = repo.commit_turn_admission
    snapshots = []
    publishers = []

    async def capture_publisher(turn):
        publishers.append(turn.emit_event)

    def held(*args, **kwargs):
        snapshots.append(deepcopy(kwargs["context_snapshot"]))
        entered.set()
        assert release.wait(5)
        return commit(*args, **kwargs)

    monkeypatch.setattr(repo, "commit_turn_admission", held)
    monkeypatch.setattr(QueryEngine, "_publish_system_prompt", staticmethod(capture_publisher))
    task = asyncio.create_task(session._run_agent("Current input", conversation_id=owner.id, metadata=metadata()))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        await publishers[0]("task.update", {"conversation_id": owner.id,
            "message_id": "current-answer", "id": "late-task", "content": "Actual concurrent event", "status": "in_progress"})
        release.set()
        await task
        await complete(session)
        assert snapshots[0]["ui_agent_state"]["todos"] == []
        fresh = ConversationRepository(repo.storage_root).get_conversation(owner.id)
        assert [item["id"] for item in fresh.context_snapshot["ui_agent_state"]["todos"]] == ["late-task"]
        assert len(adapter.requests) == 1
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await session.session_lifecycle.shutdown(reason="ui-reset-concurrent-event")
