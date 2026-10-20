"""Per-call producer identity survives file effects and late code-cell facts."""
from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest

from backend.agent.tool_execution import tool_context_with_live_output
from backend.llm.base import StreamEvent, StreamEventType, ToolCallEvent
from backend.permissions.context import PermissionContext, ToolCallSource, ToolExecutionContext
from backend.tests import test_ws_code_cell_ownership as ownership
from backend.tools.base import BaseTool, ToolSchema
from backend.tools.file_tools import EditFileTool, ReadFileTool, WriteFileTool
from backend.tools.registry import ToolRegistry


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["file.changed", "artifact.preview", "ask_user", "request_user_input"])
async def test_actual_call_emitter_stamps_owner_without_replacing_artifact_id(kind):
    class Producer(BaseTool):
        name = "event_producer"
        def get_schema(self):
            return ToolSchema(self.name, "Owned event producer", {"type": "object"})
        async def execute(self, args, context=None):
            raise AssertionError("This test invokes the admitted emitter directly")

    registry = ToolRegistry()
    registry.register(Producer())
    source = ToolCallSource("code_mode", "actual-parent", "actual-cell", "actual-runtime-call")
    seen = []
    async def receive(event, data):
        seen.append((event, data))
    context = ToolExecutionContext(permission=PermissionContext(), conversation_id="actual-thread", turn_id="actual-turn",
        call_sources={"actual-call": source}, emit_event=receive)
    call = ToolCallEvent("actual-call", "event_producer", {})
    admitted = tool_context_with_live_output(call, context, registry)
    await admitted.emit_event(kind, {"id": "artifact-or-question-id", "path": "owned.txt", "tool_call_id": "untrusted-input"})
    payload = seen[0][1]
    assert payload["id"] == "artifact-or-question-id" and payload["path"] == "owned.txt"
    assert payload["tool_call_id"] == call.id and payload["turn_id"] == "actual-turn"
    assert payload["conversation_id"] == "actual-thread"
    assert payload["call_source"] == {"kind": "code_mode", "parent_call_id": "actual-parent",
        "cell_id": "actual-cell", "runtime_call_id": "actual-runtime-call"}


@pytest.mark.asyncio
@pytest.mark.parametrize("start_next_turn", [False, True])
async def test_completed_cell_writes_and_edits_real_file_with_owned_late_facts(tmp_path, monkeypatch, start_next_turn):
    make_session = ownership.session_for
    def session_with_files(*args):
        session, conversation, adapter = make_session(*args)
        session.conversation_repo.update_permission_mode(conversation.id, "bypass")
        session.permission_context = session.permission_checker.build_context(mode="bypass", sandbox_mode="danger-full-access")
        for tool in (WriteFileTool(), ReadFileTool(session.artifact_store), EditFileTool()):
            session.tool_registry.register(tool)
        return session, conversation, adapter
    monkeypatch.setattr(ownership, "session_for", session_with_files)
    original_stream = ownership.CellModel.stream_chat
    async def script_with_real_writes(self, messages, tools=None, metadata=None):
        if self.step == 0:
            self.step += 1
            yield StreamEvent(StreamEventType.TOOL_CALL, tool_calls=[ToolCallEvent("start-cell", "tool_exec", {
                "code": "text('yielded'); await tools.held_process({}); text((await tools.write_file({file_path:'after-final.txt',content:'written original\\n'})).content); await tools.read_file({file_path:'after-final.txt'}); text((await tools.edit_file({file_path:'after-final.txt',old_string:'written original',new_string:'edited final'})).content);",
                "yield_time_ms": 1})])
            yield StreamEvent(StreamEventType.DONE, finish_reason="tool_calls")
        else:
            async for event in original_stream(self, messages, tools=tools, metadata=metadata):
                yield event
    monkeypatch.setattr(ownership.CellModel, "stream_chat", script_with_real_writes)
    async with ownership.ws_cell(tmp_path, monkeypatch) as (session, conversation, model, tool, store, cell):
        old = session.conversation_repo.get_conversation(conversation.id).transcript[-1]
        assert old["terminal_status"] == "completed"
        old_id, old_turn = old["id"], cell.parent.turn_id
        next_entered, next_release = asyncio.Event(), asyncio.Event()
        next_task = None
        async def next_turn(messages, tools=None, metadata=None):
            next_entered.set()
            await next_release.wait()
            yield StreamEvent(StreamEventType.TEXT_CHUNK, content="Independent later turn.", phase="final_answer")
            yield StreamEvent(StreamEventType.DONE, finish_reason="stop", end_turn=True)
        try:
            if start_next_turn:
                monkeypatch.setattr(model, "stream_chat", next_turn)
                await session.start_agent_run("Continue independently.", conversation_id=conversation.id,
                    metadata={"assistant_message_id": "new-answer", "user_message_id": "new-user"})
                next_task = session.run_manager.run_tasks[conversation.id]
                await asyncio.wait_for(next_entered.wait(), 5)
            expected_context = deepcopy(session.conversation_repo.get_conversation(conversation.id).context_snapshot)
            (tmp_path / "workspace/release").write_text("complete", encoding="utf-8")
            await asyncio.wait_for(asyncio.shield(cell.task), 8)
            await session.event_outbox.drain_delivery()
            assert (tmp_path / "workspace/after-final.txt").read_text(encoding="utf-8") == "edited final\n"
            assert cell.status == "completed" and cell.error == ""
            after = session.conversation_repo.get_conversation(conversation.id)
            old_message = next(message for message in after.transcript if message["id"] == old_id)
            writes = [record for record in old_message["tool_calls"] if record["name"] in {"write_file", "edit_file"}]
            assert len(writes) == 2 and all(record["status"] == "success" for record in writes)
            assert old_message["content"] == old["content"] and old_message["terminal_status"] == "completed"
            assert after.context_snapshot == expected_context
            for record in writes:
                changed = next(event for event in session.ws.messages if event.get("type") == "file.changed"
                    and event.get("tool_call_id") == record["id"])
                result = next(event for event in session.ws.messages if event.get("type") == "tool_result"
                    and event.get("id") == record["id"])
                assert changed["call_source"] == record["callSource"] == result["call_source"]
                assert changed["message_id"] == result["message_id"] == old_id
                assert changed["turn_id"] == result["turn_id"] == old_turn
                assert changed["seq"] < result["seq"]
            assert tool.process.returncode == 0 and not store.live_tool_calls()
            if start_next_turn:
                assert not next_task.done() and session.run_manager.run_tasks[conversation.id] is next_task
                assert session._conversation_streams[conversation.id]["message_id"] == "new-answer"
        finally:
            next_release.set()
            if next_task is not None:
                await asyncio.wait_for(next_task, 5)
