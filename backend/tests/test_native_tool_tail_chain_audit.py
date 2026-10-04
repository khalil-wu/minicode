"""Unaccepted native tool tails must not survive into provider continuation."""
import asyncio
from types import SimpleNamespace

import pytest

from backend.agent.context import ContextBuilder
from backend.agent.execution_journal import ExecutionJournal
from backend.agent.provider_stream_settlement import ProviderStreamResult
from backend.agent.stream_attempt import StreamAttemptState, StreamTextState
from backend.agent.streaming_tool_execution import StreamingToolExecution
from backend.agent.tool_stream_tracker import StreamingToolTracker
from backend.config import LLMSettings
from backend.llm.base import LLMMessage, ToolCallEvent, UsageInfo
from backend.llm.openai_adapter import OpenAIAdapter
from backend.llm.anthropic_adapter import AnthropicAdapter


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["responses", "custom", "anthropic"])
@pytest.mark.parametrize("accept_tail,action", [(False, "proceed"), (False, "terminate"), (True, "proceed")])
async def test_committed_native_history_and_journal_pair_on_next_wire(tmp_path, provider, accept_tail, action):
    calls = [ToolCallEvent(id=key, name="read_file", arguments={"path": key}) for key in ("A", "B")]
    committed = calls if accept_tail else calls[:1]
    if provider == "anthropic":
        reasoning = {"type": "thinking", "thinking": "reason", "signature": "opaque-signature"}
        hosted = {"type": "server_tool_use", "id": "hosted", "name": "web_search", "input": {"query": "reference"}}
        hosted_result = {"type": "web_search_tool_result", "tool_use_id": "hosted", "content": []}
        items = [{"type": "anthropic_message", "content": [reasoning, hosted, hosted_result, *[
            {"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments} for call in calls
        ]]}]
    else:
        reasoning = {"type": "reasoning", "id": "reason", "summary": [], "encrypted_content": "opaque-reasoning"}
        hosted = {"type": "web_search_call", "id": "hosted", "status": "completed"}
        items = [reasoning, hosted, *[
            {"type": "custom_tool_call" if provider == "custom" else "function_call", "id": f"item-{call.id}",
             "call_id": call.id, "name": call.name, "input" if provider == "custom" else "arguments": '{"path":"' + call.id + '"}',
             **({"status": "completed" if call.id == "A" or accept_tail else "incomplete"} if provider == "responses" else {})}
            for call in calls
        ]]
    context = ContextBuilder()
    history = context.append_assistant_tool_calls(committed)
    for call in committed:
        context._history_store.append(LLMMessage(role="tool", content=f"result {call.id}", tool_call_id=call.id, name=call.name))
    journal = ExecutionJournal("native-tail", base_dir=tmp_path)
    executor = object.__new__(StreamingToolExecution)
    executor.owner = SimpleNamespace(context_builder=context, tool_context=SimpleNamespace(run_context=SimpleNamespace(execution_journal=journal)))
    executor.calls = {call.id: call for call in committed}
    executor.history_message = history
    executor.tasks = []
    executor._events = asyncio.Queue()
    executor._closing = False
    executor._journal_message = None
    executor.iteration_id = "iteration"
    state = StreamAttemptState(tool_calls=calls, response_items=items, committed_tool_ids=set(executor.calls), provider_done=True)

    async def provider_events():
        yield ProviderStreamResult(action, state, StreamTextState(), StreamingToolTracker(), UsageInfo(), UsageInfo(), "max_output_tokens" if not accept_tail else "tool_calls", "commentary", 0)

    async for _ in executor.project(provider_events()):
        pass
    saved_message = journal.read_events()[-1].payload["message"]
    assert saved_message["provider_items"] == history.provider_items
    kept = history.provider_items[0]["content"] if provider == "anthropic" else history.provider_items
    assert reasoning in kept
    assert hosted in kept
    if provider == "anthropic":
        assert hosted_result in kept
        adapter = object.__new__(AnthropicAdapter)
        _, wire = adapter._convert_messages(context._get_history_within_budget())
        blocks = [block for message in wire if isinstance(message["content"], list) for block in message["content"]]
        call_ids = [item["id"] for item in blocks if item.get("type") == "tool_use"]
        result_ids = [item["tool_use_id"] for item in blocks if item.get("type") == "tool_result"]
        assert reasoning in blocks and hosted in blocks and hosted_result in blocks
    else:
        adapter = object.__new__(OpenAIAdapter)
        adapter._settings = LLMSettings(api_key="", provider="openai", wire_api="responses", supports_custom_tools=provider == "custom")
        wire = adapter._build_responses_input(context._get_history_within_budget())
        call_ids = [item["call_id"] for item in wire if item.get("type") in {"function_call", "custom_tool_call"}]
        result_ids = [item["call_id"] for item in wire if item.get("type") in {"function_call_output", "custom_tool_call_output"}]
        assert reasoning in wire
    assert call_ids == result_ids == [call.id for call in committed]
