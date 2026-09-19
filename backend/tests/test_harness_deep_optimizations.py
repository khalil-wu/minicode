from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.agent.execution_journal import ExecutionJournal
from backend.agent.message import AgentEvent
from backend.agent.query_journal import QueryJournalRecorder
from backend.agent.state import AgentState
from backend.conversations.models import ConversationRecord
from backend.conversations.public_projection import project_public_conversation
from backend.conversations.repository import ConversationRepository
from backend.llm.base import LLMAdapter, StreamEvent, StreamEventType
from backend.services.subagent_service import build_subagent_transcript_messages
from backend.tests.test_committed_tool_scheduling import WorkTool, committed, run_case
from backend.tools.apply_patch import ApplyPatchTool
from backend.tools.registry import ToolRegistry
from backend.ws.event_outbox import EventOutbox


@pytest.mark.parametrize("phase", ["", "commentary", "final_answer"])
@pytest.mark.parametrize("failure_kind", ["error", "exception"])
def test_text_before_transient_failure_is_replaced_and_retried(tmp_path, phase, failure_kind):
    class Provider(LLMAdapter):
        requests = 0
        async def stream_chat(self, messages, tools=None):
            self.requests += 1
            if self.requests == 1:
                yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="Discarded attempt", phase=phase)
                if failure_kind == "exception":
                    raise ConnectionError("connection reset by peer")
                yield StreamEvent(type=StreamEventType.ERROR, content="HTTP 503 service unavailable", raw={"status_code": 503})
            else:
                yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="Complete answer", phase="final_answer")
                yield StreamEvent(type=StreamEventType.DONE)
        async def simple_chat(self, messages): return ""
    async def scenario():
        provider = Provider()
        state, context, events = await run_case(tmp_path, provider, [])
        assert provider.requests == 2
        assert state.terminal_status == "completed"
        assert state.reply == "Complete answer"
        assert "Discarded attempt" not in str(context.export_snapshot()["history"])
        if phase == "commentary":
            assert any(event.type == "agent.item" and event.data.get("status") == "retracted" for event in events)
        else:
            assert any(event.type == "item.completed" and event.data["item"].get("source") == "cancelled" and event.data["item"]["text"] == "" for event in events)
    asyncio.run(scenario())


def test_committed_tool_effect_is_retained_across_text_and_disconnect(tmp_path):
    async def scenario():
        tool = WorkTool("retained_read")
        class Provider(LLMAdapter):
            requests = 0
            async def stream_chat(self, messages, tools=None):
                self.requests += 1
                if self.requests == 1:
                    yield committed(tool.name, "once")
                    await tool.started.wait()
                    yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="Tool completed", phase="commentary")
                    raise ConnectionError("connection reset by peer")
                assert any(message.role == "tool" and message.tool_call_id == "once" for message in messages)
                yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="Complete")
                yield StreamEvent(type=StreamEventType.DONE)
            async def simple_chat(self, messages): return ""
        state, _, _ = await run_case(tmp_path, Provider(), [tool])
        assert state.terminal_status == "completed" and tool.executions == 1
    asyncio.run(scenario())


def test_journal_deltas_have_linear_payload_and_reconstruct_running_text(tmp_path, monkeypatch):
    now = [1.0]
    monkeypatch.setattr("backend.agent.query_journal.time.monotonic", lambda: now[0])
    journal = ExecutionJournal("delta-audit", base_dir=tmp_path)
    recorder = QueryJournalRecorder(journal, {}, AgentState(user_message="test"), None, None, "conv_test")
    for _ in range(64):
        now[0] += .13
        recorder.record_event(AgentEvent.agent_message_delta("abcdefgh" * 128, item_id="text"))
    events = journal.read_events()
    assert sum(len(event.payload.get("content_delta", "")) for event in events) == 64 * 1024
    restored = build_subagent_transcript_messages({"agent_id": "delta-audit", "events": [event.to_dict() for event in events]})
    assert restored[0]["content"] == "abcdefgh" * (128 * 64)
    recorder.record_event(AgentEvent.agent_message_completed("Final corrected text", item_id="text", source="model_final", status="completed"))
    restored = build_subagent_transcript_messages({"events": [event.to_dict() for event in journal.read_events()]})
    assert restored[0]["blocks"][0]["content"] == "Final corrected text"


def test_tool_journal_index_is_refreshed_after_another_writer(tmp_path):
    first = ExecutionJournal("indexed", base_dir=tmp_path)
    second = ExecutionJournal("indexed", base_dir=tmp_path)
    first.append_tool_use({"id": "call", "name": "read_file", "args": {"file_path": "a.py"}})
    assert second.append_tool_result({"id": "call", "content": "read", "status": "success"}, tool_name="read_file") is not None
    assert first.append_tool_result({"id": "call", "content": "read", "status": "success"}, tool_name="read_file") is None
    assert first.append_tool_use({"id": "call", "name": "read_file", "args": {"file_path": "b.py"}}) is not None
    assert [event.event_type for event in first.read_events()] == ["tool_use", "tool_result", "tool_use"]


def test_notification_enqueue_does_not_wait_for_socket_and_preserves_order(tmp_path):
    async def scenario():
        release = asyncio.Event()
        sent = []
        class Socket:
            async def send_json(self, payload):
                await release.wait()
                sent.append(payload)
        outbox = EventOutbox(session_id="queue", websocket=Socket(), replay_root=tmp_path, replay_limit=100, cleanup_tasks=set(), has_active_run=lambda: True, requires_conversation_owner=lambda *_: True, workspace_scoped_event_types=())
        for text in ["one", "two", "three"]:
            await asyncio.wait_for(outbox.send_payload({"type": "agent_message.delta", "conversation_id": "conv_queue", "item_id": "answer", "delta": text}, log_context="test", wait_for_delivery=False), timeout=.5)
        assert not sent
        assert outbox.runtime_snapshot()["pending_delivery"] == 2
        release.set()
        await outbox.drain_delivery()
        await outbox.drain_persistence()
        assert [event["delta"] for event in sent] == ["one", "two", "three"]
        assert [event["seq"] for event in sent] == [1, 2, 3]
    asyncio.run(scenario())


def test_queued_notifications_keep_connection_generation_and_replay_ownership(tmp_path):
    async def scenario():
        release = asyncio.Event()
        class Socket:
            def __init__(self, blocked=False): self.events, self.blocked = [], blocked
            async def send_json(self, payload):
                if self.blocked: await release.wait()
                self.events.append(payload)
        old, new = Socket(True), Socket()
        outbox = EventOutbox(session_id="handoff-queue", websocket=old, replay_root=tmp_path, replay_limit=100, cleanup_tasks=set(), has_active_run=lambda: True, requires_conversation_owner=lambda *_: False, workspace_scoped_event_types=())
        for text in ["one", "two"]:
            # A durable event type: streaming deltas are live-only
            # (payload_contracts.LIVE_ONLY_EVENT_TYPES) and own no replay slot.
            await outbox.send_payload({"type": "tool_result", "conversation_id": "conv_queue", "item_id": "answer", "content": text}, log_context="test", wait_for_delivery=False)
        outbox.attach_websocket(new)
        release.set()
        await outbox.drain_delivery()
        await outbox.drain_persistence()
        assert [event["content"] for event in old.events] == ["one"]
        assert new.events == []
        assert await outbox.replay_missed_events(1) == 1
        assert new.events[0]["type"] == "session.replay"
        assert new.events[0]["events"][0]["content"] == "two"
        assert outbox.events_dropped_during_disconnect
    asyncio.run(scenario())


def test_recent_page_and_api_preserve_all_history_and_reject_deleted_anchor(tmp_path, monkeypatch):
    from backend.api import _state
    from backend.api.routes_chat import router
    repository = ConversationRepository(base_dir=tmp_path)
    conversation = repository.create_conversation(title="Paged")
    conversation.transcript = [{"id": f"m{i}", "role": "user" if i % 2 == 0 else "assistant", "content": f"message {i}"} for i in range(202)]
    repository.save_conversation(conversation)
    monkeypatch.setattr(conversation, "to_dict", lambda: pytest.fail("public paging must not copy the private context"))
    recent = project_public_conversation(conversation, transcript_limit=80)
    assert recent["message_count"] == 202 and len(recent["transcript"]) == 80
    app = FastAPI()
    app.include_router(router)
    monkeypatch.setattr(_state.ws_manager, "get_session", lambda _: SimpleNamespace(conversation_repo=repository))
    messages = recent["transcript"]
    page = recent["transcript_page"]
    with TestClient(app) as client:
        while page["has_more"]:
            response = client.get(f"/api/conversations/{conversation.id}/messages", params={"session_id": "s", "before_message_id": page["before_message_id"], "limit": 80})
            assert response.status_code == 200
            payload = response.json()
            messages = payload["transcript"] + messages
            page = payload["transcript_page"]
        assert [message["id"] for message in messages] == [f"m{i}" for i in range(202)]
        assert client.get(f"/api/conversations/{conversation.id}/messages", params={"session_id": "s", "before_message_id": "removed"}).status_code == 409


def test_actual_model_patch_contract_contains_grammar_and_example():
    registry = ToolRegistry()
    registry.register(ApplyPatchTool())
    schema = registry.get_tool_schema("apply_patch")["function"]
    assert "*** Begin Patch" in schema["description"] and "*** End Patch" in schema["description"]
    assert "@@" in schema["description"] and "actual newlines" in schema["description"]
