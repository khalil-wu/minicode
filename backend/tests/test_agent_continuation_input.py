"""Internal continuation sampling does not manufacture another user input."""
from __future__ import annotations

import asyncio

from backend.agent.query_engine import QueryEngine
from backend.llm.base import LLMAdapter, StreamEvent, StreamEventType
from backend.tests.test_harness_turn_boundaries import query_factory


def test_empty_host_continuation_preserves_prior_input_and_completes(query_factory):
    make_session, make_submission = query_factory

    class Provider(LLMAdapter):
        def __init__(self):
            self.inputs = []

        async def stream_chat(self, messages, tools=None):
            self.inputs.append(messages)
            yield StreamEvent(StreamEventType.TEXT_CHUNK, content="Actual completed answer", phase="final_answer")
            yield StreamEvent(StreamEventType.DONE)

        async def simple_chat(self, messages):
            return ""

    provider = Provider()
    session = make_session(provider)

    async def scenario():
        try:
            first = [event async for event in QueryEngine().submit(make_submission(session, "Original accepted request"))]
            continuation = make_submission(session, "")
            continuation.runtime.metadata.update({"_parent_notification_only": True, "query_source": "task-notification"})
            second = [event async for event in QueryEngine().submit(continuation)]
            assert len(provider.inputs) == 2
            assert not [event for event in first + second if event.type == "error"]
            assert [event.data["status"] for event in first + second if event.type == "done"] == ["completed", "completed"]
            admitted = [message for message in session.context_builder._history if message.is_user_input]
            assert len(admitted) == 1 and "Original accepted request" in admitted[0].content
            assert len(session.context_builder.guardian_evidence.retained.user_messages) == 1
        finally:
            await session.aclose()

    asyncio.run(scenario())
