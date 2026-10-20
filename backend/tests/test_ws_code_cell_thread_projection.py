"""A real completed WebSocket turn keeps only its actual code-cell tool facts."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json

import pytest

from backend.llm.base import StreamEvent, StreamEventType, ToolCallEvent
from backend.tests.test_ws_code_cell_ownership import CellModel, ws_cell


@pytest.mark.asyncio
@pytest.mark.parametrize("start_next_turn", [False, True])
async def test_completed_cell_publishes_real_result_to_original_turn(tmp_path, monkeypatch, start_next_turn, record_property):
    original_stream = CellModel.stream_chat

    async def start_two_calls(self, messages, tools=None, metadata=None):
        if self.step == 0:
            self.step += 1
            yield StreamEvent(StreamEventType.TOOL_CALL, tool_calls=[ToolCallEvent("start-cell", "tool_exec", {
                "code": "text('yielded'); const first=await tools.held_process({}); text(first.content); const late=await tools.held_process({}); text(late.content);",
                "yield_time_ms": 1})])
            yield StreamEvent(StreamEventType.DONE, finish_reason="tool_calls")
        else:
            async for event in original_stream(self, messages, tools=tools, metadata=metadata):
                yield event

    monkeypatch.setattr(CellModel, "stream_chat", start_two_calls)
    async with ws_cell(tmp_path, monkeypatch) as (session, conversation, model, tool, store, cell):
        first = session.conversation_repo.get_conversation(conversation.id)
        original = next(message for message in reversed(first.transcript) if message["role"] == "assistant")
        old_id, old_turn = original["id"], cell.parent.turn_id
        assert original["turn_id"] == old_turn
        original_tool = next(record for record in original["tool_calls"] if record["name"] == "held_process")
        assert original["terminal_status"] == "completed"
        assert original_tool["status"] == "running" and "error" not in original_tool
        assert original_tool["id"] in store.live_tool_calls()
        next_entered, next_release = asyncio.Event(), asyncio.Event()
        next_task = None

        async def next_turn(messages, tools=None, metadata=None):
            next_entered.set()
            await next_release.wait()
            yield StreamEvent(StreamEventType.TEXT_CHUNK, content="Independent next turn.", phase="final_answer")
            yield StreamEvent(StreamEventType.DONE, finish_reason="stop", end_turn=True)

        try:
            if start_next_turn:
                monkeypatch.setattr(model, "stream_chat", next_turn)
                await session.start_agent_run("Run the independent next turn.", conversation_id=conversation.id,
                    metadata={"assistant_message_id": "assistant-next", "user_message_id": "user-next"})
                next_task = session.run_manager.run_tasks[conversation.id]
                await asyncio.wait_for(next_entered.wait(), 5)
            expected_context = deepcopy(session.conversation_repo.get_conversation(conversation.id).context_snapshot)
            (tmp_path / "workspace/release").write_text("complete", encoding="utf-8")
            await asyncio.wait_for(asyncio.shield(cell.task), 5)
            await session.event_outbox.drain_delivery()
            after = session.conversation_repo.get_conversation(conversation.id)
            capture = tmp_path / "ws-code-cell-projection.json"
            capture.write_text(json.dumps({"initial_conversation": first.to_dict(),
                "server_events": session.ws.messages, "final_conversation": after.to_dict(),
                "cell": {"status": cell.status, "error": cell.error}}, ensure_ascii=False), encoding="utf-8")
            record_property("wire_capture", str(capture))
            old_message = next(message for message in after.transcript if message["id"] == old_id)
            old_tool = next(record for record in old_message["tool_calls"] if record["id"] == original_tool["id"])
            assert tool.process.returncode == 0 and tool.finished.is_set()
            assert old_tool["status"] == "success" and "process completed" in old_tool["summary"]
            assert old_message["content"] == original["content"]
            assert old_message["terminal_status"] == "completed"
            assert old_message["metadata"] == original["metadata"]
            assert after.context_snapshot == expected_context
            result = next(event for event in session.ws.messages if event.get("type") == "tool_result"
                and event.get("id") == original_tool["id"])
            assert result["message_id"] == old_id and result["turn_id"] == old_turn
            assert result["call_source"] == original_tool["callSource"]
            later_tool = next(record for record in old_message["tool_calls"]
                if record["name"] == "held_process" and record["id"] != original_tool["id"])
            assert later_tool["status"] == "success" and "process completed" in later_tool["summary"]
            later_start = next(event for event in session.ws.messages if event.get("type") == "tool_call"
                and event.get("id") == later_tool["id"])
            first_done = next(event for event in session.ws.messages if event.get("type") == "done"
                and event.get("message_id") == old_id)
            assert later_start["seq"] > first_done["seq"]
            assert later_start["message_id"] == old_id and later_start["turn_id"] == old_turn
            assert not store.live_tool_calls()
            if start_next_turn:
                assert not next_task.done()
                assert session.run_manager.run_tasks[conversation.id] is next_task
                assert session._conversation_streams[conversation.id]["message_id"] == "assistant-next"
                assert session._conversation_streams[conversation.id]["turn_id"] != old_turn
        finally:
            next_release.set()
            if next_task is not None:
                await asyncio.wait_for(next_task, 5)
