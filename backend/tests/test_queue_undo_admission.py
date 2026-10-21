from __future__ import annotations

import pytest

from backend.agent.message import UserCommand
from backend.tests.test_conversation_window_follower import windows


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["claimed", "admitted", "active"])
async def test_undo_cannot_recreate_an_identity_that_was_already_claimed_or_admitted(windows, state):
    _, owner, follower, conversation, _, start, drain = windows
    start()
    command = UserCommand("user_message", {"conversation_id": conversation.id, "content": "original actual input",
        "assistant_message_id": "queue-original-assistant", "user_message_id": "queue-original-user"})
    claimed = None
    if state == "claimed":
        owner.run_manager.enqueue_user_message(conversation.id, command)
        claimed = owner.run_manager.dequeue_user_message(conversation.id)
    elif state == "admitted":
        owner.conversation_repo.append_transcript_message(conversation.id, {"id": "queue-original-user", "role": "user",
            "content": "already admitted actual input"})
    else:
        owner._conversation_streams[conversation.id]["message_id"] = "queue-original-assistant"
    await follower.command_dispatcher._dispatch_client_command(UserCommand("user_message.queue.add", {
        "client_command_id": "late-undo", "conversation_id": conversation.id, "content": "must not be revived",
        "message_id": "queue-original-assistant", "user_message_id": "queue-original-user", "attachments": [],
    }), follower.connection_generation)
    await drain()
    assert owner.run_manager.queued_user_messages(conversation.id) == []
    assert any(event.get("client_command_id") == "late-undo" and event.get("level") == "error" for event in follower.ws.sent)
    assert not any(event.get("input", {}).get("content") == "must not be revived" for event in follower.ws.sent)
    if claimed is not None:
        owner.run_manager.finish_user_message_dispatch(conversation.id, claimed, succeeded=True)
