import asyncio
from threading import Event
from unittest.mock import AsyncMock

import pytest

from backend.agent.context import ContextBuilder
from backend.agent.message import UserCommand
from backend.tests.test_user_input_presentation_chain import session_for


@pytest.mark.asyncio
async def test_run_uses_complete_durable_history_while_ui_hydration_is_pending(tmp_path, monkeypatch):
    session, conversation, adapter = session_for(tmp_path, monkeypatch)
    history = [{"role": "user", "content": f"durable history {index}"} for index in range(500)]
    session.conversation_repo.save_context_snapshot(conversation.id, {"history": history})
    session.context_builder.load_snapshot({"history": history[-20:]})
    ui_wait = AsyncMock(side_effect=AssertionError("UI projection is independent of the complete execution snapshot"))
    monkeypatch.setattr(session.conversation_runtime, "wait_for_hydration", ui_wait)
    try:
        await session.command_dispatcher._handle_command_inner(UserCommand("user_message", {
            "conversation_id": conversation.id, "content": "current input",
            "user_message_id": "new-user", "assistant_message_id": "new-answer"}))
        await asyncio.gather(*tuple(session.execution_cleanup_tasks))
        assert len(adapter.requests) == 1
        rendered = "\n".join(message.content for message in adapter.requests[0])
        assert all(item["content"] in rendered for item in history)
        ui_wait.assert_not_awaited()
    finally:
        await session.session_lifecycle.shutdown(reason="independent-history")


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_history_decode_overlaps_lifecycle_and_cancellation_retains_worker(tmp_path, monkeypatch, cancel):
    session, conversation, adapter = session_for(tmp_path, monkeypatch)
    session.conversation_repo.save_context_snapshot(conversation.id,
        {"history": [{"role": "user", "content": "retained before current turn"}]})
    entered, release = Event(), Event()
    lifecycle = asyncio.Event()
    load = ContextBuilder.load_snapshot

    def held_load(builder, snapshot):
        if builder._conversation_id == conversation.id and not entered.is_set():
            entered.set()
            assert release.wait(8), "History decode was not released"
        return load(builder, snapshot)

    async def prepare(**kwargs):
        assert await asyncio.to_thread(entered.wait, 5), "History decoding should already be running"
        assert not adapter.requests
        lifecycle.set()
        return None

    monkeypatch.setattr(ContextBuilder, "load_snapshot", held_load)
    monkeypatch.setattr(session, "_ensure_lifecycle_runtime", prepare)
    task = asyncio.create_task(session._run_agent_locked("current input", conversation_id=conversation.id,
        metadata={"user_message_id": "new-user", "assistant_message_id": "new-answer"}))
    try:
        await asyncio.wait_for(lifecycle.wait(), 6)
        assert not task.done() and not adapter.requests
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert any(not pending.done() for pending in session.run_manager.lifecycle_cleanup_tasks_for(conversation.id))
        release.set()
        if not cancel:
            await asyncio.wait_for(task, 15)
            assert len(adapter.requests) == 1
        else:
            assert not adapter.requests
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.gather(*tuple(session.run_manager.lifecycle_cleanup_tasks_for(conversation.id)), return_exceptions=True)
        await session.session_lifecycle.shutdown(reason="overlapped-history")


def test_execution_resume_recovers_plan_before_model_snapshot_is_returned(tmp_path, monkeypatch):
    from backend.agent import plans
    from backend.conversations.repository import ConversationRepository

    monkeypatch.setattr(plans, "STATE_ROOT", tmp_path)
    repository = ConversationRepository(tmp_path / "conversations")
    slug = "bright-planning-lantern"
    path = plans.get_plan_file_path(slug)
    conversation = repository.create_conversation(context_snapshot={"plan_slug": slug,
        "plan_file_reference": {"path": str(path), "plan_content": "retained actual plan"},
        "history": [{"role": "user", "content": "all model history"}]},
        transcript=[{"id": "plan", "role": "assistant", "content": "", "snapshotFiles": [{"key": "plan", "content": "retained actual plan"}]}])
    assert not path.exists()
    execution = repository.get_execution_context(conversation.id, prepare_resume=True)
    assert path.read_text(encoding="utf-8") == "retained actual plan"
    assert execution.context_snapshot["plan_recovery_status"] == "recovered"
    assert execution.context_snapshot["history"] == conversation.context_snapshot["history"]
    assert execution.transcript == []


@pytest.mark.asyncio
async def test_background_model_restore_does_not_copy_public_transcript(tmp_path, monkeypatch):
    from unittest.mock import Mock
    from backend.conversations.repository import ConversationRepository
    from backend.ws.conversation_runtime import ConversationRuntime

    repository = ConversationRepository(tmp_path / "conversations")
    history = [{"role": "user", "content": f"durable model {index}"} for index in range(100)]
    record = repository.create_conversation(context_snapshot={"history": history},
        transcript=[{"id": f"ui-{index}", "role": "user", "content": "rendered public history"} for index in range(1000)])
    builder = ContextBuilder()
    runtime = ConversationRuntime(conversation_repo=repository, context_builder=builder, build_summary_from_transcript=lambda *_: "")
    runtime.active_conversation_id = record.id
    monkeypatch.setattr(repository, "get_conversation", Mock(side_effect=AssertionError("Model hydration copied UI history")))
    try:
        runtime.defer_repository_hydration(record.id, on_hydration_complete=None)
        await runtime.wait_for_hydration(record.id)
        assert [message.content for message in builder._history] == [item["content"] for item in history]
    finally:
        await runtime.shutdown()
