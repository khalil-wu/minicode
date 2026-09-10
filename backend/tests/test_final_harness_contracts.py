from __future__ import annotations

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from backend.agent.message import AgentEvent
from backend.agent.query_journal import QueryJournalRecorder
from backend.agent.state import AgentState
from backend.artifact.store import ArtifactStore
from backend.llm.base import LLMAdapter, StreamEvent, StreamEventType, ToolCallEvent
from backend.memory import consolidation_agent


def test_memory_consolidation_can_write_its_workspace_without_an_approval_channel(tmp_path, monkeypatch):
    root = tmp_path / "memories"
    root.mkdir()
    monkeypatch.setattr(consolidation_agent, "ArtifactStore", lambda: ArtifactStore(storage_dir=tmp_path / "artifacts"))

    class Provider(LLMAdapter):
        calls = 0
        results = {}
        tool_names = set()

        async def stream_chat(self, messages, tools=None):
            self.calls += 1
            self.tool_names = {tool["function"]["name"] for tool in tools or []}
            if self.calls == 1:
                yield StreamEvent(type=StreamEventType.TOOL_CALL, tool_calls=[
                    ToolCallEvent(id="memory", name="write_file", arguments={"file_path": "MEMORY.md", "content": "# Memory\nVerified preference.\n"}),
                    ToolCallEvent(id="escape", name="write_file", arguments={"file_path": "../outside.md", "content": "must not write"}),
                    ToolCallEvent(id="database", name="write_file", arguments={"file_path": "memories_1.sqlite3", "content": "must not write"}),
                ])
            else:
                self.results = {message.tool_call_id: message for message in messages if message.role == "tool"}
                yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="Memory updated within its allowed directory.")
            yield StreamEvent(type=StreamEventType.DONE)

        async def simple_chat(self, messages):
            return "Memory updated."

    provider = Provider()
    asyncio.run(consolidation_agent.run_memory_consolidation_agent(llm=provider, memory_root=root, prompt="Consolidate verified memory."))
    assert (root / "MEMORY.md").read_text(encoding="utf-8") == "# Memory\nVerified preference.\n"
    assert not provider.results["memory"].is_error
    assert provider.results["escape"].is_error and provider.results["database"].is_error
    assert not (tmp_path / "outside.md").exists()
    assert not (root / "memories_1.sqlite3").exists()
    assert not {"run_command", "task", "web_fetch"} & provider.tool_names


@pytest.mark.parametrize("status", ["completed", "interrupted"])
def test_journal_coalesces_bursts_but_flushes_final_text_immediately(monkeypatch, status):
    now = [10.0]
    monkeypatch.setattr("backend.agent.query_journal.time.monotonic", lambda: now[0])
    records = []
    journal = SimpleNamespace(append=lambda kind, payload: records.append((kind, deepcopy(payload))))
    recorder = QueryJournalRecorder(journal, {}, AgentState(user_message="stream"), None, None, "conversation")
    for _ in range(256):
        recorder.record_event(AgentEvent(type="agent_message.delta", data={"item_id": "answer", "delta": "x" * 32}))
    assert len(records) == 1
    now[0] += 0.13
    recorder.record_event(AgentEvent(type="agent_message.delta", data={"item_id": "answer", "delta": " tick"}))
    assert len(records) == 2
    assert records[-1][1]["content"] == "x" * 8192 + " tick"
    final = "x" * 8192 + " tick final"
    recorder.record_event(AgentEvent(type="item.completed", data={"item": {"type": "agent_message", "id": "answer", "text": final, "status": status}}))
    assert len(records) == 3
    assert records[-1][1]["content"] == final
    assert records[-1][1]["status"] == status
