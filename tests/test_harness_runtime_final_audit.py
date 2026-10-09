from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from backend.agent.context import ContextBuilder
from backend.agent.execution_journal import ExecutionJournal
from backend.agent.loop import run_agent_loop
from backend.agent.message import AgentEvent
from backend.agent.query_journal import QueryJournalRecorder
from backend.agent.run_context import RunContext
from backend.agent.runtime import AgentRuntime
from backend.agent.state import AgentState
from backend.artifact.store import ArtifactStore
from backend.config import AgentSettings, PermissionSettings, TokenBudget
from backend.llm.base import LLMAdapter, StreamEvent, StreamEventType, ToolCallEvent
from backend.permissions.checker import PermissionChecker
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema
from backend.tools.registry import ToolRegistry


def test_stream_journal_reduces_deltas_without_a_worker_per_token(tmp_path, monkeypatch):
    from backend.agent import query_journal

    clock = [10.0]
    monkeypatch.setattr(query_journal.time, "monotonic", lambda: clock[0])
    worker_calls = []

    async def worker(function, *args, **kwargs):
        worker_calls.append(function.__name__)
        return function(*args, **kwargs)

    monkeypatch.setattr(query_journal, "to_thread_cancel_safe", worker)
    journal = ExecutionJournal("stream-receipts", base_dir=tmp_path)
    recorder = QueryJournalRecorder(journal, {}, AgentState(user_message="audit"),
        ContextBuilder(), None, "audit")

    async def run():
        await recorder.record_event_async(AgentEvent.agent_message_started(item_id="answer", source="pending"))
        for _ in range(1000):
            await recorder.record_event_async(AgentEvent.agent_message_delta("x", item_id="answer"))
        await recorder.record_event_async(AgentEvent(type="thinking_delta", data={"delta": "reasoning"}))
        assert worker_calls == ["append"]
        clock[0] += 0.13
        await recorder.record_event_async(AgentEvent.agent_message_delta("tail", item_id="answer", source="model_final"))
        await recorder.record_event_async(AgentEvent.agent_message_completed(
            "x" * 1000 + "tail", item_id="answer", source="model_final", status="completed"))

    asyncio.run(run())
    progress = [event.payload for event in journal.read_events() if event.event_type == "progress"]
    assert progress[0]["content_delta"] == "x"
    assert progress[1]["content_offset"] == 1
    assert progress[1]["content_delta"] == "x" * 999 + "tail"
    assert progress[2]["content"] == "x" * 1000 + "tail"
    assert worker_calls == ["append", "append", "record_event"]


class _WriteTool(BaseTool):
    name = "audit_write"
    permission = PermissionLevel.AUTO
    mutates_workspace = True

    def __init__(self):
        self.executed = asyncio.Event()
        self.calls = 0

    def get_schema(self):
        return ToolSchema(name=self.name, description="Apply one change", parameters={"type": "object", "properties": {}})

    async def execute(self, args, context=None):
        self.calls += 1
        self.executed.set()
        return ToolResult(content="change applied")


class _AfterToolFailureLLM(LLMAdapter):
    def __init__(self, tool, failure):
        self.tool = tool
        self.failure = failure
        self.calls = 0
        self.next_history = []

    async def stream_chat(self, messages, tools=None):
        self.calls += 1
        if self.calls == 1:
            yield StreamEvent(type=StreamEventType.TOOL_CALL,
                tool_calls=[ToolCallEvent(id="write-once", name=self.tool.name, arguments={})],
                tool_calls_committed=True, tool_calls_final=False)
            await self.tool.executed.wait()
            if self.failure == "event":
                yield StreamEvent(type=StreamEventType.ERROR, content="gateway connection reset", raw={"status_code": 502})
            else:
                raise ConnectionError("gateway connection reset")
            return
        self.next_history = list(messages)
        yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="continued from tool result")
        yield StreamEvent(type=StreamEventType.DONE)

    async def simple_chat(self, messages):
        return "unused"


async def _run(tmp_path, llm, registry=None, metadata=None, runtime=None, context=None, settings=None):
    runtime = runtime or AgentRuntime(metrics_file=tmp_path / "metrics.jsonl",
        swarm_store_dir=tmp_path / "swarm", enable_lease_heartbeat=False)
    state = AgentState(user_message="audit", conversation_id="audit-conversation")
    context = context or ContextBuilder(llm=llm)
    spans = []

    async def emit(event_type, data):
        spans.append((event_type, data))

    events = [event async for event in run_agent_loop(user_message="audit", llm=llm,
        tool_registry=registry or ToolRegistry(), artifact_store=ArtifactStore(),
        permission_checker=PermissionChecker(PermissionSettings(), tmp_path),
        agent_settings=settings or AgentSettings(max_iterations=4, stream_retry_delay_seconds=0),
        token_budget=TokenBudget(), state=state, context_builder=context,
        metadata=metadata if metadata is not None else {},
        run_context=RunContext(agent_runtime=runtime), emit_event=emit)]
    return events, state, context, spans


@pytest.mark.parametrize("failure", ["event", "exception"])
def test_committed_tool_failure_preserves_the_real_error_and_resumes_once(tmp_path, failure):
    async def run():
        tool = _WriteTool()
        registry = ToolRegistry()
        registry.register(tool)
        llm = _AfterToolFailureLLM(tool, failure)
        events, state, context, spans = await _run(tmp_path, llm, registry)
        assert tool.calls == 1
        assert llm.calls == 2
        assert state.reply == "continued from tool result"
        assert any(message.role == "tool" and "change applied" in message.content for message in llm.next_history)
        failed_spans = [data for kind, data in spans
            if kind == "runtime.span" and data.get("event") == "provider.request.failed"]
        expected_detail = "gateway connection reset" if failure == "event" else "provider=network"
        assert any(expected_detail in data.get("data", {}).get("error_message", "") for data in failed_spans)
        assert any(event.type == "agent.progress" and expected_detail in event.data.get("error_message", "") for event in events)
        assert not any(event.type == "error" and event.data.get("error_code") == "provider_terminal_missing" for event in events)

    asyncio.run(run())


def test_independent_streamed_reads_have_no_unrequested_ten_call_ceiling(tmp_path, monkeypatch):
    monkeypatch.delenv("MINICODE_MAX_TOOL_CONCURRENCY", raising=False)

    async def run():
        class ReadTool(BaseTool):
            name = "audit_read"
            permission = PermissionLevel.AUTO
            read_only = True

            def __init__(self):
                self.started = 0
                self.all_started = asyncio.Event()

            def get_schema(self):
                return ToolSchema(name=self.name, description="Read independent data",
                    parameters={"type": "object", "properties": {}})

            async def execute(self, args, context=None):
                self.started += 1
                if self.started == 12:
                    self.all_started.set()
                await self.all_started.wait()
                return ToolResult(content="read completed")

        tool = ReadTool()

        class ReadLLM(LLMAdapter):
            def __init__(self):
                self.calls = 0

            async def stream_chat(self, messages, tools=None):
                self.calls += 1
                if self.calls == 1:
                    yield StreamEvent(type=StreamEventType.TOOL_CALL,
                        tool_calls=[ToolCallEvent(id=f"read-{index}", name=tool.name, arguments={}) for index in range(12)],
                        tool_calls_committed=True)
                    await tool.all_started.wait()
                    yield StreamEvent(type=StreamEventType.DONE)
                    return
                yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="all reads collected")
                yield StreamEvent(type=StreamEventType.DONE)

            async def simple_chat(self, messages):
                return "unused"

        registry = ToolRegistry()
        registry.register(tool)
        events, state, _, _ = await asyncio.wait_for(_run(tmp_path, ReadLLM(), registry), timeout=5)
        assert tool.started == 12
        assert state.reply == "all reads collected"
        assert len([event for event in events if event.type == "tool_result"]) == 12

    asyncio.run(run())


class _CoordinationDuringAnswerLLM(LLMAdapter):
    def __init__(self, runtime, metadata, kind):
        self.runtime = runtime
        self.metadata = metadata
        self.kind = kind
        self.calls = 0
        self.next_history = []

    async def stream_chat(self, messages, tools=None):
        self.calls += 1
        if self.calls == 1:
            child = self.runtime.start_subagent(subagent_id="audit-child", agent_type="general",
                parent_run_id=self.runtime.latest_main_run("audit-conversation").run_id, background=True)
            yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="draft before child finished")
            if self.kind == "mailbox":
                self.runtime.send_swarm_message(sender_id=child.subagent_id, recipient_id="parent",
                    conversation_id="audit-conversation", content="new child evidence",
                    sender_mailbox_epoch=child.mailbox_epoch)
            else:
                self.runtime.complete_subagent(child.subagent_id, agent_path=child.agent_path, mailbox_epoch=child.mailbox_epoch)
                self.runtime.store_subagent_result(child.subagent_id, status="completed", content="new child evidence",
                    agent_path=child.agent_path, mailbox_epoch=child.mailbox_epoch)
            yield StreamEvent(type=StreamEventType.DONE)
            return
        self.next_history = list(messages)
        yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="answer incorporating child evidence")
        yield StreamEvent(type=StreamEventType.DONE)

    async def simple_chat(self, messages):
        return "unused"


@pytest.mark.parametrize("kind", ["mailbox", "completion"])
def test_final_boundary_consumes_coordination_arriving_during_sampling(tmp_path, kind):
    async def run():
        runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl",
            swarm_store_dir=tmp_path / "swarm", enable_lease_heartbeat=False)
        metadata = {}
        llm = _CoordinationDuringAnswerLLM(runtime, metadata, kind)
        events, state, context, _ = await _run(tmp_path, llm, metadata=metadata, runtime=runtime)
        assert llm.calls == 2
        assert state.reply == "answer incorporating child evidence"
        draft_indexes = [index for index, message in enumerate(llm.next_history)
            if message.role == "assistant" and message.content == "draft before child finished"]
        evidence_indexes = [index for index, message in enumerate(llm.next_history)
            if message.role == "user" and "new child evidence" in message.content]
        assert len(draft_indexes) == 1
        assert len(evidence_indexes) == 1
        assert draft_indexes[0] < evidence_indexes[0]
        assert len([event for event in events if event.type == "done"]) == 1
        if kind == "completion":
            notifications = runtime.list_parent_notifications(conversation_id="audit-conversation")
            assert notifications[0]["status"] == "acked"

    asyncio.run(run())


def test_reactive_compaction_exception_reaches_the_existing_runtime_failure_owner(tmp_path):
    class BrokenCompactionContext(ContextBuilder):
        async def full_compact(self, restore_state=None):
            raise ValueError("compaction summary parser failed; api_key=private-audit-secret")

    class OverflowLLM(LLMAdapter):
        def __init__(self):
            self.calls = 0

        async def stream_chat(self, messages, tools=None):
            self.calls += 1
            yield StreamEvent(type=StreamEventType.ERROR, content="prompt is too long",
                raw={"error_type": "prompt_too_long", "provider_error_type": "prompt_too_long"})

        async def simple_chat(self, messages):
            return "unused"

    async def run():
        llm = OverflowLLM()
        context = BrokenCompactionContext(llm=llm)
        events, state, _, spans = await _run(tmp_path, llm, context=context)
        errors = [event for event in events if event.type == "error"]
        assert llm.calls == 1
        assert state.stopped_reason == "runtime_error"
        assert state.terminal_status == "failed"
        assert any("compaction summary parser failed" in event.data["message"] for event in errors)
        assert not any("private-audit-secret" in event.data["message"] for event in errors)
        assert any(kind == "runtime.span" and data.get("event") == "provider.request.failed"
            and "compaction summary parser failed" in data.get("data", {}).get("error_message", "")
            for kind, data in spans)
        assert len([event for event in events if event.type == "done"]) == 1

    asyncio.run(run())


def test_transport_switch_occurs_after_exhaustion_then_resets_the_same_budget(tmp_path):
    class SwitchingLLM(LLMAdapter):
        def __init__(self):
            self.calls = 0
            self.switched_at = []

        def try_fallback_transport(self):
            if self.switched_at:
                return False
            self.switched_at.append(self.calls)
            return True

        async def stream_chat(self, messages, tools=None):
            self.calls += 1
            if self.calls < 6:
                yield StreamEvent(type=StreamEventType.ERROR, content="HTTP 503 unavailable",
                    raw={"retry_after_seconds": 0})
            else:
                yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="HTTPS completed")
                yield StreamEvent(type=StreamEventType.DONE)

        async def simple_chat(self, messages):
            return "unused"

    async def run():
        llm = SwitchingLLM()
        events, state, _, spans = await _run(tmp_path, llm,
            settings=AgentSettings(max_iterations=4, stream_max_attempts=2, stream_retry_delay_seconds=0))
        assert llm.switched_at == [3]
        assert llm.calls == 6
        assert state.reply == "HTTPS completed"
        started = [data for kind, data in spans if kind == "runtime.span" and data.get("event") == "provider.request.started"]
        assert [data["data"]["retry_attempt"] for data in started] == [0, 1, 2, 0, 1, 2]
        assert len({data["span_id"] for data in started}) == 6
        assert len([event for event in events if event.type == "done"]) == 1

    asyncio.run(run())


@pytest.mark.parametrize("source", ["user", "background"])
def test_capacity_retries_follow_server_advice_without_a_role_fuse(tmp_path, source):
    class BusyLLM(LLMAdapter):
        def __init__(self):
            self.calls = 0

        async def stream_chat(self, messages, tools=None):
            self.calls += 1
            if self.calls <= 4:
                yield StreamEvent(type=StreamEventType.ERROR, content="HTTP 529 overloaded",
                    raw={"status_code": 529, "retry_after_seconds": 0})
            else:
                yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="capacity restored")
                yield StreamEvent(type=StreamEventType.DONE)

        async def simple_chat(self, messages):
            return "unused"

    async def run():
        llm = BusyLLM()
        _, state, _, _ = await _run(tmp_path, llm, metadata={"query_source": source})
        assert llm.calls == 5
        assert state.reply == "capacity restored"

    asyncio.run(run())
