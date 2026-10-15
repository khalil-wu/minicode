"""The original run clock survives both real resume producers and paging."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from backend.agent.message import AgentEvent
from backend.ws.approval_runtime import SessionApprovalRuntimeMixin
from backend.ws.conversation_stream import frozen_stream_pages
from backend.ws.payload_contracts import MAX_STREAM_RESUME_BLOCKS
from backend.ws.stream_state import apply_stream_event, create_stream_state


OWNER = "clock-conversation"
MESSAGE = "clock-assistant"
TURN = "clock-turn"
STARTED_AT = 1_792_041_671_198


def _running_state(*, paged: bool) -> dict:
    state = create_stream_state(OWNER, MESSAGE, TURN)
    streams = {OWNER: state}
    apply_stream_event(streams, OWNER, "agent.run.started", {
        "conversation_id": OWNER, "message_id": MESSAGE, "turn_id": TURN,
        "role": "main", "started_at": STARTED_AT,
    })
    # Neither child work nor a previous turn owns this clock.
    apply_stream_event(streams, OWNER, "agent.run.started", {
        "conversation_id": OWNER, "message_id": MESSAGE, "turn_id": TURN,
        "role": "subagent", "started_at": STARTED_AT + 9_000,
    })
    apply_stream_event(streams, OWNER, "agent.run.started", {
        "conversation_id": OWNER, "message_id": "older-assistant", "turn_id": "older-turn",
        "role": "main", "started_at": STARTED_AT - 9_000,
    })
    state["content_blocks"] = [
        {"type": "text", "itemId": f"clock-item-{index}", "content": str(index)}
        for index in range(MAX_STREAM_RESUME_BLOCKS + 1 if paged else 1)
    ]
    return state


@pytest.mark.parametrize("paged", [False, True])
def test_owner_and_follower_resume_preserve_the_original_run_clock(paged):
    async def run():
        state = _running_state(paged=paged)
        observed = [page.to_ws_message() for page in frozen_stream_pages(OWNER, state)]

        class Owner(SessionApprovalRuntimeMixin):
            ws_manager = None

            async def send_event(self, event):
                self.sent.append(event.to_ws_message())

        pending = asyncio.create_task(asyncio.Event().wait())
        owner = Owner()
        owner.sent = []
        owner._conversation_streams = {OWNER: state}
        owner.run_manager = SimpleNamespace(run_tasks={OWNER: pending})
        try:
            await owner.reemit_pending_state(OWNER)
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)

        assert len(owner.sent) == len(observed) == (2 if paged else 1)
        for pages in (owner.sent, observed):
            assert {page["started_at"] for page in pages} == {STARTED_AT}
            assert {page["message_id"] for page in pages} == {MESSAGE}
            assert {page["turn_id"] for page in pages} == {TURN}
            if paged:
                assert [page["snapshot_part"] for page in pages] == [0, 1]
                assert [page["snapshot_complete"] for page in pages] == [False, True]
        assert state["started_at"] == STARTED_AT

    asyncio.run(run())


def test_resume_without_an_original_start_does_not_invent_a_clock():
    state = create_stream_state(OWNER, MESSAGE, TURN)
    packet = frozen_stream_pages(OWNER, state)[0].to_ws_message()
    assert "started_at" not in packet


def test_an_explicit_epoch_start_is_preserved_instead_of_becoming_missing():
    state = create_stream_state(OWNER, MESSAGE, TURN)
    apply_stream_event({OWNER: state}, OWNER, "agent.run.started", {
        "conversation_id": OWNER, "message_id": MESSAGE, "turn_id": TURN,
        "role": "main", "started_at": 0,
    })
    assert frozen_stream_pages(OWNER, state)[0].to_ws_message()["started_at"] == 0
