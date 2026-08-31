from __future__ import annotations

import asyncio

from backend.agent.conversation_query_guard import ConversationQueryGuardRegistry
from backend.agent.message import AgentEvent
from backend.api.models import ChatRequest
from backend.services import chat_api_service


def test_chat_request_carries_durable_conversation_owner() -> None:
    request = ChatRequest(message="hello", conversation_id="conv-rest")

    assert request.conversation_id == "conv-rest"


def test_owned_query_events_closes_stream_when_generation_is_lost(monkeypatch) -> None:
    guards = ConversationQueryGuardRegistry()
    monkeypatch.setattr(chat_api_service, "conversation_query_guards", lambda: guards)
    claim = guards.try_start("conv-rest", owner_id="rest:first")
    assert claim is not None
    closed = False

    async def source():
        nonlocal closed
        try:
            yield AgentEvent.progress("first")
            guards.end(claim)
            replacement = guards.try_start("conv-rest", owner_id="ws:replacement")
            assert replacement is not None
            yield AgentEvent.progress("stale")
        finally:
            closed = True

    async def scenario() -> list[AgentEvent]:
        return [event async for event in chat_api_service._owned_query_events(source(), claim)]

    events = asyncio.run(scenario())

    assert [event.type for event in events] == ["agent.progress"]
    assert closed is True
    active = guards.active_claim("conv-rest")
    assert active is not None
    assert active.owner_id == "ws:replacement"
