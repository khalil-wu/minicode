from __future__ import annotations

from contextlib import aclosing

import pytest

from backend.agent.context import ContextBuilder
from backend.agent.state import AgentState
from backend.config import AppConfig, LLMSettings
from backend.llm.base import LLMAdapter, StreamEvent, StreamEventType, UsageInfo
from backend.permissions.context import PermissionContext
from backend.sdk import create_tool_registry, query


class AnswerProvider(LLMAdapter):
    def __init__(self, phase: str):
        self.phase = phase
        self.requests = []

    async def stream_chat(self, messages, tools=None, metadata=None):
        self.requests.append(messages)
        yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="Accepted answer", phase=self.phase)
        yield StreamEvent(type=StreamEventType.DONE, finish_reason="completed", usage=UsageInfo())

    async def simple_chat(self, messages, *, max_tokens=None):
        raise AssertionError("No auxiliary request is expected")


def answer_messages(context):
    return [message for message in context.export_snapshot()["history"]
            if message.get("role") == "assistant" and message.get("content") == "Accepted answer"]


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["", "final_answer"])
@pytest.mark.parametrize("close_on_answer", [True, False])
async def test_public_final_answer_is_retained_before_sdk_publication(tmp_path, phase, close_on_answer):
    context = ContextBuilder()
    state = AgentState(user_message="Give an answer")
    config = AppConfig(llm=LLMSettings(api_key=""))
    provider = AnswerProvider(phase)
    published = False
    async with aclosing(query(
        "Give an answer", llm=provider, tool_registry=create_tool_registry(), config=config,
        state=state, context_builder=context, workspace_root=tmp_path,
        permission_context=PermissionContext(mode="bypass"), session_id="publication-chain",
    )) as stream:
        async for event in stream:
            item = event.data.get("item") or {}
            if event.type == "item.completed" and item.get("source") == "model_final":
                published = True
                assert state.reply == "Accepted answer"
                assert len(answer_messages(context)) == 1
                if close_on_answer:
                    break

    assert published
    assert state.reply == "Accepted answer"
    assert len(answer_messages(context)) == 1
    assert answer_messages(context)[0]["phase"] == "final_answer"
    assert state.terminal_status == ("cancelled" if close_on_answer else "completed")

    followup = AnswerProvider(phase)
    async with aclosing(query(
        "Continue from the answer", llm=followup, tool_registry=create_tool_registry(), config=config,
        context_builder=context, workspace_root=tmp_path,
        permission_context=PermissionContext(mode="bypass"), session_id="publication-chain",
    )) as stream:
        async for _ in stream:
            pass
    assert any(message.role == "assistant" and message.content == "Accepted answer"
               for message in followup.requests[0])
