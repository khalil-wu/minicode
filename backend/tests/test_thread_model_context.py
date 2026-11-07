"""A completed thread transfers its model context only while durable state agrees."""
import asyncio
from copy import deepcopy

import pytest

from backend.agent.context import ContextBuilder
from backend.agent.run_context import RunContext
from backend.tests.test_user_input_presentation_chain import session_for
from backend.ws.execution_context import ConversationThreadRuntime


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["history", "metadata", "workspace", "local_history", "borrower", "presentation"])
async def test_retained_context_checks_durable_content_workspace_and_pending_borrowers(change):
    builder = ContextBuilder()
    builder.append_user("original")
    snapshot = builder.export_snapshot()
    thread = ConversationThreadRuntime("store", "thread")
    thread.retain_model_context(builder, snapshot, "workspace")
    incoming = deepcopy(snapshot)
    workspace = "workspace"
    pending = None
    if change == "history":
        incoming["history"][0]["content"] = "external replacement"
    elif change == "metadata":
        incoming["extension_state"] = {"changed": True}
    elif change == "workspace":
        workspace = "other"
    elif change == "local_history":
        builder.append_user("late local mutation")
    elif change == "borrower":
        pending = asyncio.create_task(asyncio.Event().wait())
        thread.retain_turn(pending, set())
    else:
        incoming["ui_agent_state"] = {"expanded": True}
        incoming["_ui_agent_state_revision"] = 9
    try:
        taken = thread.take_model_context(incoming, workspace)
        assert (taken is builder) == (change == "presentation")
        assert thread.model_context is None
    finally:
        if pending is not None:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        await thread.close_environments()


@pytest.mark.asyncio
async def test_real_ws_turns_reuse_context_and_external_replacement_forces_reload(tmp_path, monkeypatch):
    session, conversation, adapter = session_for(tmp_path, monkeypatch)
    thread = ConversationThreadRuntime(session.conversation_repo.store_instance_id(), conversation.id)
    snapshots = []
    load = ContextBuilder.load_snapshot

    def observed(builder, snapshot):
        if builder._conversation_id == conversation.id:
            snapshots.append(builder)
        return load(builder, snapshot)

    monkeypatch.setattr(ContextBuilder, "load_snapshot", observed)
    owners = []

    async def turn(number):
        run = RunContext(environment_thread_runtime=thread)
        await asyncio.create_task(session._run_agent(f"input {number}", conversation_id=conversation.id,
            run_context=run, metadata={"user_message_id": f"user-{number}", "assistant_message_id": f"answer-{number}"}))
        await asyncio.gather(*tuple(session.run_manager.lifecycle_cleanup_tasks_for(conversation.id)))
        assert thread.model_context is not None
        owners.append(thread.model_context[0])

    try:
        await turn(1)
        await turn(2)
        assert owners[0] is owners[1]
        assert snapshots == [owners[0]]
        assert any("input 1" in str(message.content) for message in adapter.requests[1])
        assert any("fixture answer" in str(message.content) for message in adapter.requests[1])
        saved = session.conversation_repo.get_execution_context(conversation.id)
        replaced = deepcopy(saved.context_snapshot)
        replaced["history"][0]["content"] = "external history replacement"
        session.conversation_repo.save_context_snapshot(conversation.id, replaced)
        await turn(3)
        assert owners[2] is not owners[1]
        assert any("external history replacement" in str(message.content) for message in adapter.requests[2])
        assert len(snapshots) == 2
    finally:
        await thread.close_environments()
        await session.session_lifecycle.shutdown(reason="thread-model-context")
