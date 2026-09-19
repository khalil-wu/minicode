from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backend.agent.context import ContextBuilder
from backend.agent.state import AgentState
from backend.config import AgentSettings
from backend.conversations.context_delta import apply_context_snapshot_delta
from backend.conversations.repository import ConversationRepository
from backend.llm.base import LLMMessage, ToolCallEvent
from backend.memory.generation import MemoryGenerationCoordinator, schedule_memory_startup
from backend.tools.base import BaseTool, ToolSchema, ToolResult, PermissionLevel
from backend.tools.contracts import ToolSpec
from backend.tools.registry import ToolRegistry
from backend.tools.tool_search import ToolSearchTool
from backend.ws.conversation_runtime import ConversationRuntime


def history(count):
    return [{"id": f"m-{i}", "role": "assistant" if i % 2 else "user", "content": f"{i}: 中文😀 " + "x" * 2048} for i in range(count)]


def test_cold_pages_and_inventory_do_not_read_the_private_checkpoint(tmp_path, monkeypatch):
    writer = ConversationRepository(tmp_path)
    record = writer.create_conversation(transcript=history(400), context_snapshot={"history": history(400)})
    reader = ConversationRepository(tmp_path)
    monkeypatch.setattr(reader, "_read_snapshot_path", lambda *a, **k: pytest.fail("private context was read"))
    monkeypatch.setattr(reader, "_read_transcript_path", lambda *a, **k: pytest.fail("whole transcript was read"))
    assert reader.list_conversations()[0].message_count == 400
    page = reader.get_conversation_view(record.id)
    assert len(page["transcript"]) == 80
    collected = page["transcript"]
    while page["transcript_page"]["has_more"]:
        page = reader.get_conversation_view(record.id, before_message_id=page["transcript_page"]["before_message_id"])
        collected = page["transcript"] + collected
    assert [message["id"] for message in collected] == [f"m-{i}" for i in range(400)]
    assert collected[399]["content"].startswith("399: 中文😀")


def test_indexed_page_combines_current_partial_projection_and_metadata(tmp_path):
    repo = ConversationRepository(tmp_path)
    record = repo.create_conversation(transcript=history(200), context_snapshot={"history": []})
    partial = repo.commit_turn_projection(record.id, assistant_message={"id": "live", "role": "assistant", "content": "new result"},
        context_delta={"set": {}, "removed": [], "history_from": 0, "history": [{"role": "assistant", "content": "new result"}]}, partial=True, expected_revision=record.revision)
    repo.rename_conversation(record.id, "renamed while running")
    view = ConversationRepository(tmp_path).get_conversation_view(record.id)
    assert view["title"] == "renamed while running"
    assert view["message_count"] == 201
    assert view["transcript"][-1]["id"] == "live"
    assert view["transcript"][-1]["content"] == "new result"
    assert view["revision"] > partial.revision


def test_indexed_read_uses_existing_generation_recovery_for_a_damaged_transcript(tmp_path):
    repo = ConversationRepository(tmp_path)
    original = repo.create_conversation(transcript=history(120), context_snapshot={"history": history(120)})
    changed = repo.append_transcript_message(original.id, {"id": "new", "role": "user", "content": "new"})
    repo.transcript_path(original.id).write_text("broken JSON\n", encoding="utf-8")
    reader = ConversationRepository(tmp_path)
    view = reader.get_conversation_view(original.id)
    assert view["transcript"][-1]["id"] == "m-119"
    assert view["revision"] == original.revision < changed.revision
    assert reader.get_conversation_summary(original.id).revision == original.revision
    with pytest.raises(ValueError, match="anchor"):
        reader.get_conversation_view(original.id, before_message_id="missing")


def test_snapshot_delta_serializes_only_changed_messages_and_survives_rewrite(tmp_path, monkeypatch):
    import backend.agent.context as module
    builder = ContextBuilder(conversation_id="test", workspace_root=tmp_path)
    for i in range(200): builder._history_store.append(LLMMessage(role="user", content=str(i)))
    before = builder.export_snapshot()
    revision = builder._history_store.revision
    serialize = Mock(wraps=module._sanitize_provider_items)
    monkeypatch.setattr(module, "_sanitize_provider_items", serialize)
    builder.append_assistant("new")
    delta, next_revision = builder.export_snapshot_delta(before, since_revision=revision)
    assert serialize.call_count == 1
    assert delta["history_from"] == 200
    after = apply_context_snapshot_delta(before, delta)
    assert after == builder.export_snapshot()
    # The caller did not commit its first candidate: the old cursor still
    # produces both changes rather than dropping the unacknowledged append.
    builder.append_assistant("another")
    retry, _ = builder.export_snapshot_delta(before, since_revision=revision)
    assert apply_context_snapshot_delta(before, retry) == builder.export_snapshot()
    builder._history = [LLMMessage(role="user", content="replacement")]
    rewritten, _ = builder.export_snapshot_delta(after, since_revision=next_revision)
    assert rewritten["history_from"] == 0
    assert apply_context_snapshot_delta(after, rewritten) == builder.export_snapshot()


def test_mutated_tool_group_is_in_the_snapshot_delta(tmp_path):
    builder = ContextBuilder(conversation_id="test", workspace_root=tmp_path)
    builder.append_user("work")
    message = builder.append_assistant_tool_calls([ToolCallEvent(id="a", name="read_file", arguments={"file_path": "a"})])
    before, revision = builder.export_snapshot(), builder._history_store.revision
    builder.append_assistant_tool_calls([ToolCallEvent(id="b", name="read_file", arguments={"file_path": "b"})], message=message)
    delta, _ = builder.export_snapshot_delta(before, since_revision=revision)
    assert delta["history_from"] == 1
    assert [call["id"] for call in delta["history"][0]["tool_calls"]] == ["a", "b"]
    assert apply_context_snapshot_delta(before, delta) == builder.export_snapshot()


def test_incremental_ledger_matches_rebuild_and_clone_keeps_its_own_counters(tmp_path):
    from copy import deepcopy
    from backend.agent.context import clone_context_builder
    builder = ContextBuilder(workspace_root=tmp_path)
    builder._history_store.append(LLMMessage(role="user", content="image", images=[{"media_type": "image/png", "data": "aGVsbG8="}]))
    builder._history_store.append(LLMMessage(role="tool", name="read_file", content="result"))
    changed = builder._history[-1]
    changed.content = "a much longer result " * 20
    builder._history_store.refresh_message_estimate(changed)
    rebuilt = ContextBuilder(workspace_root=tmp_path)
    rebuilt._history = deepcopy(builder._history)
    assert builder.context_ledger() == rebuilt.context_ledger()
    clone = clone_context_builder(builder)
    clone.append_user("only in the clone")
    assert builder.context_ledger() == rebuilt.context_ledger()
    assert clone.context_ledger()["entries"] != builder.context_ledger()["entries"]


def test_repository_hydration_waits_for_full_context_without_cancelling_on_waiter_stop(tmp_path, monkeypatch):
    async def scenario():
        repo = ConversationRepository(tmp_path)
        record = repo.create_conversation(transcript=history(120), context_snapshot={"history": history(120)})
        entered, release = threading.Event(), threading.Event()
        original = repo.get_conversation
        calls = 0
        def load(owner):
            nonlocal calls
            calls += 1
            if calls == 1:
                entered.set()
                assert release.wait(5)
            return original(owner)
        monkeypatch.setattr(repo, "get_conversation", load)
        builder = ContextBuilder(workspace_root=tmp_path)
        runtime = ConversationRuntime(conversation_repo=repo, context_builder=builder, build_summary_from_transcript=lambda *_: "")
        runtime.active_conversation_id = record.id
        completed = []
        async def callback(owner): completed.append(owner)
        runtime.defer_repository_hydration(record.id, on_hydration_complete=callback)
        runtime.start_hydration(record.id)
        assert await asyncio.to_thread(entered.wait, 5)
        waiter = asyncio.create_task(runtime.wait_for_hydration(record.id))
        await asyncio.sleep(0)
        assert not waiter.done() and builder.history_length == 0
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError): await waiter
        assert not runtime._hydration_task.cancelled()
        release.set()
        await runtime.wait_for_hydration(record.id)
        assert builder.history_length == 120 and completed == [record.id]
        await runtime.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize("fail_read", [False, True])
def test_large_conversation_switch_publishes_page_before_private_context_load(tmp_path, monkeypatch, fail_read):
    from backend.tests.test_ws_cold_connection import _connection, _release_session
    from backend.ws.manager import WebSocketManager
    from backend.ws.handlers.conversation import handle_conversation_switch
    async def scenario():
        connection = _connection(tmp_path, session_id="paged-switch")
        manager = WebSocketManager()
        session, _ = await manager.connect(**connection)
        monkeypatch.setattr(session, "refresh_llm_selection", lambda **_: None)
        repo = session.conversation_repo
        record = repo.create_conversation(transcript=history(160), context_snapshot={"history": history(160)})
        repo._record_cache.clear()
        entered, release = threading.Event(), threading.Event()
        preview = asyncio.Event()
        original_get = repo.get_conversation
        original_send = connection["websocket"].send_json
        async def send(payload):
            await original_send(payload)
            if payload.get("context_pending"):
                assert not entered.is_set()
                preview.set()
        def load(owner):
            if owner == record.id and not release.is_set():
                entered.set()
                assert release.wait(5)
                if fail_read: raise RuntimeError("private checkpoint unreadable")
            return original_get(owner)
        monkeypatch.setattr(connection["websocket"], "send_json", send)
        monkeypatch.setattr(repo, "get_conversation", load)
        switching = asyncio.create_task(handle_conversation_switch(session, {"conversation_id": record.id}))
        try:
            await asyncio.wait_for(preview.wait(), timeout=3)
            assert await asyncio.to_thread(entered.wait, 3)
            assert session.context_builder.history_length == 0
            release.set()
            await switching
            if fail_read:
                with pytest.raises(RuntimeError, match="hydration failed"):
                    await session.conversation_runtime.wait_for_hydration(record.id)
                assert any(event.get("type") == "error" and "unreadable" in event.get("message", "") for event in connection["websocket"].sent)
            else:
                await session.conversation_runtime.wait_for_hydration(record.id)
                assert session.context_builder.history_length == 160
                switches = [event for event in connection["websocket"].sent if event.get("type") == "conversation.switched"]
                assert switches[0]["is_hydrating"] and not switches[-1]["is_hydrating"]
        finally:
            release.set()
            await session.conversation_runtime.shutdown()
            await _release_session(session)
    asyncio.run(scenario())


def test_turn_admissions_survive_delta_and_rebase_with_compaction(tmp_path, monkeypatch):
    builder = ContextBuilder(workspace_root=tmp_path, agent_settings=AgentSettings(compaction_keep_recent_tokens=4))
    builder.append_user("old request " * 20)
    builder.append_assistant("old response " * 20)
    builder.record_turn_admission("old", {"history_start": 0, "history_end": 1})
    builder.append_user("keep")
    builder.record_turn_admission("keep", {"history_start": 2, "history_end": 3})
    before = builder.export_snapshot()
    revision = builder._history_store.revision
    async def summarize(*args, **kwargs): return "summary"
    monkeypatch.setattr(builder, "_summarize_early", summarize)
    asyncio.run(builder.compact())
    delta, _ = builder.export_snapshot_delta(before, since_revision=revision)
    after = apply_context_snapshot_delta(before, delta)
    position = next(i for i, message in enumerate(after["history"]) if message["content"] == "keep")
    assert after["turn_admissions"] == {"keep": {"history_start": position, "history_end": position + 1}}
    restored = ContextBuilder(workspace_root=tmp_path)
    restored.load_snapshot(after)
    assert restored.export_snapshot()["turn_admissions"] == after["turn_admissions"]


def test_two_compactions_restore_live_command_handle_cursor_and_latest_exit(tmp_path, monkeypatch):
    builder = ContextBuilder(conversation_id="conv", workspace_root=tmp_path, agent_settings=AgentSettings(compaction_keep_recent_tokens=1))
    state = AgentState(user_message="finish", conversation_id="conv", workspace_root=tmp_path)
    state.record_tool_call("run_command", {"command": "build"}, "running", command_id="owned-command", output_cursor=64)
    command = {"command_id": "owned-command", "status": "running", "command": "build", "cwd": str(tmp_path), "exit_code": None, "output_path": "output.txt"}
    def commands(*, include_completed, conversation_id):
        assert include_completed and conversation_id == "conv"
        return [dict(command)]
    builder.bind_background_commands(SimpleNamespace(list_commands=commands))
    async def summarize(*args, **kwargs): return "Summary without command identifiers."
    monkeypatch.setattr(builder, "_summarize_early", summarize)
    builder.append_user("start work")
    for i in range(2):
        builder.append_assistant("working")
        builder.append_user(f"continue {i}")
        if i: command.update(status="completed", exit_code=0)
        asyncio.run(builder.compact(restore_state=state))
        notes = builder.export_snapshot()["persistent_notes"]
        restored = next(note["content"] for note in notes if note["kind"] == "post_compaction_structured_state")
        assert '"command_id": "owned-command"' in restored
        assert '"next_cursor": 64' in restored
        assert f'"status": "{command["status"]}"' in restored


def test_memory_startup_is_coalesced_and_waits_without_cancelling_foreground(tmp_path, monkeypatch):
    async def scenario():
        release = asyncio.Event()
        foreground = asyncio.create_task(release.wait())
        entered = []
        monkeypatch.setattr(MemoryGenerationCoordinator, "_scoped_conversations", lambda _: entered.append(True) or [])
        async def phase2(_): pass
        monkeypatch.setattr(MemoryGenerationCoordinator, "_run_phase2", phase2)
        llm = object()
        options = dict(repository=object(), llm=llm, workspace_root=tmp_path, current_conversation_id="conv", foreground_tasks=lambda: [foreground])
        task = schedule_memory_startup(**options)
        assert schedule_memory_startup(**options) is task
        await asyncio.sleep(0)
        assert not entered
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        assert not foreground.cancelled()
        task = schedule_memory_startup(**options)
        release.set()
        await foreground
        await task
        assert entered == [True]
    asyncio.run(scenario())


def test_memory_loads_full_transcripts_only_for_selected_candidates(tmp_path, monkeypatch):
    from datetime import UTC, datetime, timedelta
    from backend.conversations.models import ConversationSummary
    old = (datetime.now(UTC) - timedelta(hours=7)).isoformat()
    summaries = [ConversationSummary(id=f"conv_memory_{i}", title="memory", created_at=old, updated_at=old,
        revision=1, content_revision=1, content_updated_at=old, message_count=1, workspace_root=str(tmp_path)) for i in range(100)]
    by_id = {item.id: item for item in summaries}
    reads = []
    def load(owner):
        reads.append(owner)
        return SimpleNamespace(**vars(by_id[owner]), transcript=[{"role": "user", "content": "Remember this fact"}])
    repo = SimpleNamespace(list_conversations=lambda: summaries, get_conversation=load,
                           get_conversation_summary=lambda owner: by_id[owner], transcript_path=lambda owner: tmp_path / f"{owner}.jsonl")
    class Provider:
        async def simple_chat(self, messages):
            return json.dumps({"raw_memory": "fact", "rollout_summary": "summary", "rollout_slug": "fact"})
    coordinator = MemoryGenerationCoordinator(repository=repo, llm=Provider(), workspace_root=tmp_path)
    async def phase2(): pass
    monkeypatch.setattr(coordinator, "_run_phase2", phase2)
    asyncio.run(coordinator.run_startup(current_conversation_id="active"))
    assert len(reads) == 2


def test_tool_search_reuses_index_and_rebuilds_on_registry_change(tmp_path, monkeypatch):
    class Searchable(BaseTool):
        name = "searchable"
        permission = PermissionLevel.AUTO
        read_only = True
        def get_schema(self): return ToolSchema(name=self.name, description="browse inspect browser", parameters={"type": "object", "properties": {}})
        def get_spec(self): return ToolSpec(name=self.name, capability="browser.inspect", exposure="deferred", toolset="browser")
        async def execute(self, args, context=None): return ToolResult(content="ok")
    registry = ToolRegistry()
    registry.register(Searchable())
    tool = ToolSearchTool(registry)
    views = Mock(wraps=registry.build_schema_views)
    monkeypatch.setattr(registry, "build_schema_views", views)
    async def scenario():
        first = await tool.execute({"query": "browser"})
        assert "searchable" in json.loads(first.content)["matches"]
        await tool.execute({"query": "inspect"})
        assert views.call_count == 1
        other = Searchable(); other.name = "another"
        registry.register(other)
        assert "another" in json.loads((await tool.execute({"query": "browser"})).content)["matches"]
        assert views.call_count == 2
    asyncio.run(scenario())
