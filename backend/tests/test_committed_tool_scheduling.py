from __future__ import annotations

import asyncio
import json
import threading
from contextvars import ContextVar

import pytest

from backend.agent.context import ContextBuilder
from backend.agent.execution_journal import ExecutionJournal
from backend.agent.loop_session import AgentLoopSessionContext
from backend.agent.query_engine import AgentSession, QueryEngine, QuerySubmission
from backend.agent.run_context import RunContext
from backend.agent.runtime import AgentRuntime
from backend.agent.state import AgentState
from backend.artifact.store import ArtifactStore
from backend.config import AgentSettings, PermissionSettings, TokenBudget, permission_settings_from_config
from backend.llm.base import LLMAdapter, StreamEvent, StreamEventType, ToolCallEvent, ToolCallStartEvent
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema
from backend.tools.registry import ToolRegistry


class WorkTool(BaseTool):
    permission = PermissionLevel.AUTO
    read_only = True

    def __init__(self, name, *, concurrent=True, release=None):
        self.name = name
        self.concurrent = concurrent
        self.release = release
        self.started = asyncio.Event()
        self.finished = asyncio.Event()
        self.executions = 0

    def get_schema(self):
        return ToolSchema(name=self.name, description="Fixture work", parameters={"type": "object", "properties": {}})

    def is_concurrency_safe(self, args=None):
        return self.concurrent

    async def execute(self, args, context=None):
        self.executions += 1
        self.started.set()
        try:
            if self.release is not None:
                await self.release.wait()
        finally:
            self.finished.set()
        return ToolResult(content=f"{self.name} finished")


def committed(name, call_id):
    return StreamEvent(type=StreamEventType.TOOL_CALL, tool_calls=[ToolCallEvent(id=call_id, name=name, arguments={})], tool_calls_final=False, tool_calls_committed=True)


async def run_case(tmp_path, llm, tools, observe=None, cancel_event=None, approval=None, stream_max_attempts=1, hooks=None, metadata=None, retry_policy=None):
    artifacts = ArtifactStore(storage_dir=str(tmp_path / "artifacts"))
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    budget = TokenBudget(total=32768, response_reserve=4096)
    ctx = ContextBuilder(token_budget=budget, llm=llm, conversation_id="conv_scheduling", workspace_root=tmp_path)
    settings = AgentSettings(max_iterations=4, max_turn_seconds=15, stream_max_attempts=stream_max_attempts, turn_error_budget=2,
        stream_retry_policy=retry_policy)
    permissions = PermissionSettings(require_confirm=[tool.name for tool in tools]) if approval else permission_settings_from_config({})
    checker = PermissionChecker(permissions, tmp_path)
    session = AgentSession(llm=llm, tool_registry=registry, artifact_store=artifacts, permission_checker=checker, agent_settings=settings, token_budget=budget, context_builder=ctx, approval_handler=approval)
    runtime = AgentRuntime(enable_lease_heartbeat=False)
    state = AgentState(user_message="Complete the fixture work", conversation_id="conv_scheduling", workspace_root=tmp_path)
    events = []
    try:
        async with asyncio.timeout(10):
            async for event in QueryEngine().submit(QuerySubmission(user_message=state.user_message, session=session, state=state, runtime=AgentLoopSessionContext(workspace_root=tmp_path, session_id="schedule-test", permission_context=checker.build_context(mode="confirm" if approval else "bypass"), cancel_event=cancel_event, metadata=metadata or {}, run_context=RunContext(agent_runtime=runtime, hook_manager=hooks, execution_journal=ExecutionJournal("schedule-test", base_dir=tmp_path / "journals"))))):
                events.append(event)
                if observe:
                    observe(event)
    except asyncio.CancelledError:
        if cancel_event is None or not cancel_event.is_set():
            raise
    finally:
        runtime.close(release_lease=True)
    return state, ctx, events


def test_native_context_and_tool_use_are_durable_before_execution(tmp_path):
    async def scenario():
        class InspectJournal(WorkTool):
            async def execute(self, args, context=None):
                journal = context.run_context.execution_journal
                events = journal.read_events()
                assert any(event.payload.get("lifecycle") == "provider_item_committed" for event in events)
                assert any(event.event_type == "tool_use" for event in events)
                recovered = journal.reconstruct_history()
                assistant = next(item for item in recovered if item.get("tool_calls"))
                assert assistant["provider_items"][0]["encrypted_content"] == "retained-context"
                assert len([item for item in recovered if item.get("tool_calls")]) == 1
                return await super().execute(args, context)
        tool = InspectJournal("inspect_committed_state")
        class Provider(LLMAdapter):
            calls = 0
            async def stream_chat(self, messages, tools=None):
                self.calls += 1
                if self.calls == 1:
                    event = committed(tool.name, "durable")
                    event.provider_items = [{"type":"reasoning", "encrypted_content":"retained-context"}]
                    yield event
                    await asyncio.wait_for(tool.started.wait(), timeout=2)
                    yield StreamEvent(type=StreamEventType.DONE, finish_reason="tool_calls")
                else:
                    yield StreamEvent(type=StreamEventType.TEXT_CHUNK,content="Finished")
                    yield StreamEvent(type=StreamEventType.DONE)
            async def simple_chat(self, messages): return ""
        state, _, _ = await run_case(tmp_path, Provider(), [tool])
        assert state.terminal_status == "completed"
        assert tool.executions == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("method", ["append_lifecycle", "append_tool_use"])
def test_journal_wait_keeps_event_loop_live_and_precedes_tool_execution(tmp_path, monkeypatch, method):
    entered, release = threading.Event(), threading.Event()
    original = getattr(ExecutionJournal, method)

    def slow_write(self, *args, **kwargs):
        if method == "append_tool_use" or args[0] == "provider_item_committed":
            entered.set()
            if not release.wait(3):
                raise AssertionError("Journal write blocked the event loop from releasing it")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(ExecutionJournal, method, slow_write)

    async def scenario():
        tool = WorkTool("journal_probe")

        class Provider(LLMAdapter):
            calls = 0

            async def stream_chat(self, messages, tools=None):
                self.calls += 1
                if self.calls == 1:
                    yield committed(tool.name, "journal-probe")
                    yield StreamEvent(type=StreamEventType.DONE, finish_reason="tool_calls")
                else:
                    yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="Finished")
                    yield StreamEvent(type=StreamEventType.DONE)

            async def simple_chat(self, messages):
                return ""

        run = asyncio.create_task(run_case(tmp_path, Provider(), [tool]))
        async with asyncio.timeout(5):
            while not entered.is_set():
                await asyncio.sleep(.001)
            assert not tool.started.is_set()
            release.set()
            state, _, _ = await run
        assert state.terminal_status == "completed"
        assert tool.executions == 1

    asyncio.run(scenario())


def test_cancelled_blocking_write_finishes_before_owner_exits(tmp_path):
    from backend.async_cleanup import to_thread_cancel_safe

    async def scenario():
        entered, release = threading.Event(), threading.Event()
        receipt = tmp_path / "committed.txt"

        def write():
            entered.set()
            release.wait(3)
            receipt.write_text("committed", encoding="utf-8")

        owner = asyncio.create_task(to_thread_cancel_safe(write))
        while not entered.is_set():
            await asyncio.sleep(.001)
        owner.cancel()
        await asyncio.sleep(.01)
        assert not owner.done()
        assert not receipt.exists()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await owner
        assert receipt.read_text(encoding="utf-8") == "committed"

    asyncio.run(scenario())


def test_failed_tool_use_journal_ack_prevents_execution(tmp_path, monkeypatch):
    def failed_write(self, *args, **kwargs):
        raise OSError("fixture journal write failed")
    monkeypatch.setattr(ExecutionJournal, "append_tool_use", failed_write)
    async def scenario():
        tool = WorkTool("must_not_execute")
        class Provider(LLMAdapter):
            async def stream_chat(self, messages, tools=None):
                yield committed(tool.name, "blocked-write")
                yield StreamEvent(type=StreamEventType.DONE,finish_reason="tool_calls")
            async def simple_chat(self, messages): return ""
        state, _, _ = await run_case(tmp_path, Provider(), [tool])
        assert tool.executions == 0
        assert state.terminal_status != "completed"
    asyncio.run(scenario())


def test_closed_read_items_execute_concurrently_before_provider_done_and_keep_result_order(tmp_path):
    async def scenario():
        a_release, b_release, b_result = asyncio.Event(), asyncio.Event(), asyncio.Event()
        a, b = WorkTool("read_first", release=a_release), WorkTool("read_second", release=b_release)
        context_value = ContextVar("stream-fixture", default="outside")

        class Provider(LLMAdapter):
            calls = 0
            overlapped = False

            async def stream_chat(self, messages, tools=None):
                token = context_value.set("inside")
                try:
                    self.calls += 1
                    if self.calls == 1:
                        yield committed(a.name, "a")
                        await asyncio.wait_for(a.started.wait(), timeout=2)
                        yield committed(b.name, "b")
                        await asyncio.wait_for(b.started.wait(), timeout=2)
                        self.overlapped = True
                        b_release.set()
                        await asyncio.wait_for(b_result.wait(), timeout=2)
                        a_release.set()
                        # Final batch echoes must not dispatch either call again.
                        yield StreamEvent(type=StreamEventType.TOOL_CALL, tool_calls=[ToolCallEvent(id="a", name=a.name, arguments={}), ToolCallEvent(id="b", name=b.name, arguments={})])
                        yield StreamEvent(type=StreamEventType.DONE, finish_reason="tool_calls")
                    else:
                        assert [m.tool_call_id for m in messages if m.role == "tool"] == ["a", "b"]
                        yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="Finished", phase="final_answer")
                        yield StreamEvent(type=StreamEventType.DONE)
                finally:
                    context_value.reset(token)

            async def simple_chat(self, messages):
                return ""

        llm = Provider()
        def observe(event):
            if event.type == "tool_result" and event.data.get("id") == "b":
                b_result.set()
        state, ctx, events = await run_case(tmp_path, llm, [a,b], observe)
        assert llm.overlapped
        assert a.executions == b.executions == 1
        assert state.terminal_status == "completed"
        assert context_value.get() == "outside"
        assert len([m for m in ctx._history if m.role == "assistant" and m.tool_calls]) == 1
        assert any(e.type == "runtime.span" and e.data.get("event") == "tool.queued" and e.data.get("status") == "completed" for e in events)

    asyncio.run(scenario())


def test_cancellation_drains_started_tool_and_does_not_start_queued_write(tmp_path):
    async def scenario():
        cancel, queued = asyncio.Event(), asyncio.Event()
        first = WorkTool("blocking_read", release=asyncio.Event())
        second = WorkTool("queued_write", concurrent=False)
        class Provider(LLMAdapter):
            async def stream_chat(self, messages, tools=None):
                yield committed(first.name, "first")
                await asyncio.wait_for(first.started.wait(), timeout=2)
                yield committed(second.name, "second")
                await asyncio.wait_for(queued.wait(), timeout=2)
                cancel.set()
                await asyncio.Event().wait()
            async def simple_chat(self, messages): return ""
        def observe(event):
            if event.type == "runtime.span" and event.data.get("tool_call_id") == "second" and event.data.get("event") == "tool.queued":
                queued.set()
        state, ctx, events = await run_case(tmp_path, Provider(), [first,second], observe, cancel)
        assert first.finished.is_set()
        assert second.executions == 0
        assert state.terminal_status == "cancelled"
        assert any(event.type == "tool_result" and event.data.get("id") == "second" and event.data.get("status") == "cancelled" for event in events)
        assert {message.tool_call_id for message in ctx._get_history_within_budget() if message.role == "tool"} == {"first", "second"}
        assert not any(task.get_name().startswith("tool:") and not task.done() for task in asyncio.all_tasks())
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["auth", "changed_call"])
def test_committed_call_protocol_or_auth_failure_never_replays_the_action(tmp_path, failure):
    async def scenario():
        tool = WorkTool("once_only", concurrent=False)
        class Provider(LLMAdapter):
            calls = 0
            async def stream_chat(self, messages, tools=None):
                self.calls += 1
                yield committed(tool.name, "once")
                await asyncio.wait_for(tool.started.wait(), timeout=2)
                if failure == "auth":
                    yield StreamEvent(type=StreamEventType.ERROR, content="Request rejected", raw={"provider_error_type":"auth","status_code":401,
                        "provider_error_message": "Workspace policy denied this request"})
                else:
                    changed = committed(tool.name, "once")
                    changed.tool_calls[0].arguments = {"changed":True}
                    yield changed
                yield StreamEvent(type=StreamEventType.DONE)
            async def simple_chat(self, messages): return ""
        provider = Provider()
        state, _, events = await run_case(tmp_path, provider, [tool])
        assert provider.calls == 1
        assert tool.executions == 1
        assert state.terminal_status == "failed"
        if failure == "auth":
            detail, = [event for event in events if event.type == "agent.progress" and event.data.get("error_message")]
            assert detail.data["error_message"] == "Workspace policy denied this request"
            assert any(event.type == "error" for event in events)
    asyncio.run(scenario())


@pytest.mark.parametrize("allowed", [True, False])
def test_streamed_item_obeys_approval_before_execution(tmp_path, allowed):
    async def scenario():
        requested = asyncio.Event()
        tool = WorkTool("reviewed_work", concurrent=False)
        async def approval(call_id):
            assert tool.executions == 0
            requested.set()
            return {"action":"approve" if allowed else "reject"}
        class Provider(LLMAdapter):
            calls = 0
            async def stream_chat(self, messages, tools=None):
                self.calls += 1
                if self.calls == 1:
                    yield committed(tool.name, "approved")
                    await asyncio.wait_for(requested.wait(), timeout=2)
                    yield StreamEvent(type=StreamEventType.DONE, finish_reason="tool_calls")
                else:
                    yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="Handled approval decision")
                    yield StreamEvent(type=StreamEventType.DONE)
            async def simple_chat(self, messages): return ""
        state, _, _ = await run_case(tmp_path, Provider(), [tool], approval=approval)
        assert requested.is_set()
        assert tool.executions == int(allowed)
        assert state.terminal_status == "completed"
    asyncio.run(scenario())


def test_serial_item_waits_for_prior_reads_and_blocks_later_reads(tmp_path):
    async def scenario():
        release = asyncio.Event()
        a, write, b = WorkTool("read_before", release=release), WorkTool("write_barrier", concurrent=False), WorkTool("read_after")
        class Provider(LLMAdapter):
            calls = 0
            async def stream_chat(self, messages, tools=None):
                self.calls += 1
                if self.calls == 1:
                    yield committed(a.name, "a")
                    await asyncio.wait_for(a.started.wait(), timeout=2)
                    yield committed(write.name, "w")
                    yield committed(b.name, "b")
                    release.set()
                    yield StreamEvent(type=StreamEventType.DONE, finish_reason="tool_calls")
                else:
                    yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="Finished")
                    yield StreamEvent(type=StreamEventType.DONE)
            async def simple_chat(self, messages): return ""
        state, _, events = await run_case(tmp_path, Provider(), [a,write,b])
        starts = [e.data.get("tool_call_id") for e in events if e.type == "runtime.span" and e.data.get("event") == "tool.queued" and e.data.get("status") == "completed"]
        results = [e.data.get("id") for e in events if e.type == "tool_result"]
        assert starts == ["a", "w", "b"]
        assert results == ["a", "w", "b"]
        assert state.terminal_status == "completed"
    asyncio.run(scenario())


def test_committed_tools_close_narration_in_timeline_order(tmp_path):
    async def scenario():
        first, second = WorkTool("first_read"), WorkTool("second_read")
        class Provider(LLMAdapter):
            calls = 0
            async def stream_chat(self, messages, tools=None):
                self.calls += 1
                if self.calls == 1:
                    yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="Inspect first.")
                    yield committed(first.name, "first")
                    await asyncio.wait_for(first.started.wait(), timeout=2)
                    yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="Inspect second.")
                    yield committed(second.name, "second")
                    yield StreamEvent(type=StreamEventType.DONE, finish_reason="tool_calls")
                else:
                    yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="Finished")
                    yield StreamEvent(type=StreamEventType.DONE)
            async def simple_chat(self, messages): return ""
        state, _, events = await run_case(tmp_path, Provider(), [first,second])
        completed = [event.data["item"] for event in events if event.type == "item.completed"]
        narration = [item for item in completed if item.get("source") == "commentary"]
        assert [item.get("text") or item.get("content") for item in narration] == ["Inspect first.", "Inspect second."]
        assert len({item["id"] for item in narration}) == 2
        first_tool = next(i for i, event in enumerate(events) if event.type == "tool_call" and event.data.get("id") == "first")
        second_narration = next(i for i, event in enumerate(events) if event.type == "item.started" and event.data.get("item", {}).get("id") == narration[1]["id"])
        assert first_tool < second_narration
        assert state.terminal_status == "completed"
    asyncio.run(scenario())


@pytest.mark.parametrize("ending", ["error", "eof", "length"])
def test_stream_failure_after_closed_tool_continues_from_results_without_repeating_it(tmp_path, ending):
    async def scenario():
        tool = WorkTool("perform_once", concurrent=False)
        class Provider(LLMAdapter):
            calls = 0
            async def stream_chat(self, messages, tools=None):
                self.calls += 1
                if self.calls == 1:
                    event = committed(tool.name, "once")
                    event.provider_items = [{"type": "reasoning", "encrypted_content": "fixture-opaque"}]
                    yield event
                    await asyncio.wait_for(tool.started.wait(), timeout=2)
                    if ending == "error":
                        yield StreamEvent(type=StreamEventType.ERROR, content="HTTP 503 service unavailable")
                    elif ending == "length":
                        yield StreamEvent(type=StreamEventType.TOOL_CALL_START, tool_call_start=ToolCallStartEvent(id="unfinished", name=tool.name))
                        yield StreamEvent(type=StreamEventType.DONE, finish_reason="length")
                else:
                    assert any(m.role == "tool" and m.tool_call_id == "once" for m in messages)
                    assert any(m.provider_items and m.provider_items[0].get("encrypted_content") == "fixture-opaque" for m in messages)
                    yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="Recovered")
                    yield StreamEvent(type=StreamEventType.DONE)
            async def simple_chat(self, messages): return ""
        provider = Provider()
        state, _, _ = await run_case(tmp_path, provider, [tool])
        assert provider.calls == 2
        assert tool.executions == 1
        assert state.terminal_status == "completed"
    asyncio.run(scenario())


@pytest.mark.parametrize("ending", ["error", "eof", "exception"])
@pytest.mark.parametrize("retries", [0, 1, 2])
def test_committed_stream_failures_honor_the_request_retry_limit(tmp_path, ending, retries):
    async def scenario():
        tool = WorkTool("perform_once", concurrent=False)

        class Provider(LLMAdapter):
            calls = 0

            async def stream_chat(self, messages, tools=None):
                self.calls += 1
                if self.calls > 1:
                    assert len([message for message in messages if message.role == "tool"]) == self.calls - 1
                yield committed(tool.name, f"call-{self.calls}")
                if ending == "error":
                    yield StreamEvent(type=StreamEventType.ERROR, content="HTTP 503 service unavailable")
                elif ending == "exception":
                    raise ConnectionResetError("connection reset by peer")

            async def simple_chat(self, messages):
                return ""

        provider = Provider()
        state, context, events = await run_case(tmp_path, provider, [tool], stream_max_attempts=retries)
        assert provider.calls == tool.executions == retries + 1
        assert state.terminal_status == "failed"
        assert len([message for message in context._history if message.role == "tool"]) == retries + 1
        assert len([event for event in events if event.type == "tool_result"]) == retries + 1
        assert any(event.type == "error" for event in events)
        assert state.total_retries == 0

    asyncio.run(scenario())


def test_retry_before_committed_tool_and_after_it_share_the_same_budget(tmp_path):
    async def scenario():
        tool = WorkTool("perform_once", concurrent=False)

        class Provider(LLMAdapter):
            calls = 0

            async def stream_chat(self, messages, tools=None):
                self.calls += 1
                if self.calls == 2:
                    yield committed(tool.name, "once")
                yield StreamEvent(type=StreamEventType.ERROR, content="HTTP 503 service unavailable")

            async def simple_chat(self, messages):
                return ""

        provider = Provider()
        state, context, _ = await run_case(tmp_path, provider, [tool], stream_max_attempts=1)
        assert provider.calls == 2 and tool.executions == 1
        assert state.terminal_status == "failed"
        assert any(message.role == "tool" and message.tool_call_id == "once" for message in context._history)

    asyncio.run(scenario())


@pytest.mark.parametrize("cancel_wait", [False, True])
def test_committed_retry_honors_server_delay_after_tool_results_and_can_be_cancelled(tmp_path, monkeypatch, cancel_wait):
    monkeypatch.setattr("backend.agent.provider_stream_runtime.monotonic", lambda: 100)
    monkeypatch.setattr("backend.agent.provider_stream_error_event.monotonic", lambda: 100)
    async def scenario():
        tool = WorkTool("perform_once", concurrent=False)
        cancel = asyncio.Event()
        waits = []
        results = []

        async def wait(delay, event):
            assert results == ["once"] and tool.executions == 1
            assert event is cancel
            waits.append(delay)
            if cancel_wait:
                cancel.set()
                raise asyncio.CancelledError

        monkeypatch.setattr("backend.agent.provider_stream_error_event.sleep_or_cancel", wait)

        class Provider(LLMAdapter):
            calls = 0

            async def stream_chat(self, messages, tools=None):
                self.calls += 1
                if self.calls == 1:
                    yield committed(tool.name, "once")
                    yield StreamEvent(type=StreamEventType.ERROR, content="HTTP 429 rate limit",
                        raw={"status_code": 429, "retry_after_seconds": 0.75})
                else:
                    assert any(message.role == "tool" and message.tool_call_id == "once" for message in messages)
                    yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="Recovered")
                    yield StreamEvent(type=StreamEventType.DONE)

            async def simple_chat(self, messages):
                return ""

        def observe(event):
            if event.type == "tool_result":
                results.append(event.data["id"])

        provider = Provider()
        state, context, _ = await run_case(tmp_path, provider, [tool], observe=observe,
            cancel_event=cancel, stream_max_attempts=1)
        assert waits == [0.75]
        assert provider.calls == (1 if cancel_wait else 2)
        if cancel_wait:
            assert state.terminal_status != "completed"
        else:
            assert state.terminal_status == "completed"
        assert any(message.role == "tool" and message.tool_call_id == "once" for message in context._history)

    asyncio.run(scenario())


@pytest.mark.parametrize("fallback", [False, True])
def test_committed_recovery_uses_custom_policy_and_transport_fallback(tmp_path, monkeypatch, fallback):
    from backend.agent.policies.stream_retry import StreamRetryDecision
    monkeypatch.setattr("backend.agent.provider_stream_runtime.monotonic", lambda: 100)
    monkeypatch.setattr("backend.agent.provider_stream_error_event.monotonic", lambda: 100)

    async def scenario():
        tool = WorkTool("perform_once", concurrent=False)
        decisions, waits = [], []

        class Policy:
            def decide_retry(self, message, attempt_index, **kwargs):
                decisions.append((message, attempt_index))
                return StreamRetryDecision(not fallback, 0.125, 1)

        async def wait(delay, _cancel):
            assert tool.finished.is_set()
            waits.append(delay)

        monkeypatch.setattr("backend.agent.provider_stream_error_event.sleep_or_cancel", wait)

        class Provider(LLMAdapter):
            calls = 0
            switched = 0

            def try_fallback_transport(self):
                self.switched += 1
                return fallback and self.switched == 1

            async def stream_chat(self, messages, tools=None):
                self.calls += 1
                if self.calls == 1:
                    yield committed(tool.name, "once")
                    yield StreamEvent(type=StreamEventType.ERROR, content="HTTP 503 service unavailable",
                        raw={"retry_after_seconds": 0.75} if fallback else {})
                else:
                    assert any(message.role == "tool" and message.tool_call_id == "once" for message in messages)
                    yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="Recovered")
                    yield StreamEvent(type=StreamEventType.DONE)

            async def simple_chat(self, messages):
                return ""

        provider = Provider()
        state, _, _ = await run_case(tmp_path, provider, [tool], stream_max_attempts=0 if fallback else 1, retry_policy=Policy())
        assert len(decisions) == 1 and decisions[0][1] == 0
        assert waits == [0.75 if fallback else 0.125]
        assert provider.calls == 2 and tool.executions == 1
        assert provider.switched == int(fallback)
        assert state.terminal_status == "completed"

    asyncio.run(scenario())


def test_fatal_provider_error_drains_started_tool_and_retains_its_journal_result(tmp_path):
    async def scenario():
        release = asyncio.Event()
        tool = WorkTool("finish_write", concurrent=False, release=release)

        class Provider(LLMAdapter):
            calls = 0

            async def stream_chat(self, messages, tools=None):
                self.calls += 1
                yield committed(tool.name, "write")
                await tool.started.wait()
                try:
                    yield StreamEvent(type=StreamEventType.ERROR, content="Invalid API key", raw={"status_code": 401})
                finally:
                    release.set()

            async def simple_chat(self, messages):
                return ""

        provider = Provider()
        state, context, events = await run_case(tmp_path, provider, [tool])
        assert provider.calls == tool.executions == 1
        assert state.terminal_status == "failed" and tool.finished.is_set()
        result_index = next(i for i, event in enumerate(events) if event.type == "tool_result" and event.data["id"] == "write")
        error_index = next(i for i, event in enumerate(events) if event.type == "error")
        assert result_index < error_index
        assert any(message.role == "tool" and message.tool_call_id == "write"
            and "finish_write finished" in str(message.content) for message in context._history)
        saved = ExecutionJournal("schedule-test", base_dir=tmp_path / "journals").read_events()
        assert any(event.event_type == "tool_result" and event.payload["tool_call_id"] == "write"
            and event.payload["status"] == "success" for event in saved)

    asyncio.run(scenario())


def test_server_retry_deadline_can_expire_while_the_original_tool_drains(tmp_path, monkeypatch):
    async def scenario():
        now = [100.0]
        monkeypatch.setattr("backend.agent.provider_stream_runtime.monotonic", lambda: now[0])
        monkeypatch.setattr("backend.agent.provider_stream_error_event.monotonic", lambda: now[0])
        release = asyncio.Event()
        waits = []

        class Tool(WorkTool):
            async def execute(self, args, context=None):
                result = await super().execute(args, context)
                now[0] += 2
                return result

        tool = Tool("finish_write", concurrent=False, release=release)

        async def wait(delay, _cancel):
            assert tool.finished.is_set()
            waits.append(delay)

        monkeypatch.setattr("backend.agent.provider_stream_error_event.sleep_or_cancel", wait)

        class Provider(LLMAdapter):
            calls = 0

            async def stream_chat(self, messages, tools=None):
                self.calls += 1
                if self.calls == 1:
                    yield committed(tool.name, "write")
                    await tool.started.wait()
                    try:
                        yield StreamEvent(type=StreamEventType.ERROR, content="HTTP 503 service unavailable",
                            raw={"retry_after_seconds": 0.75})
                    finally:
                        release.set()
                else:
                    yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="Recovered")
                    yield StreamEvent(type=StreamEventType.DONE)

            async def simple_chat(self, messages):
                return ""

        provider = Provider()
        state, _, _ = await run_case(tmp_path, provider, [tool])
        assert waits == [0]
        assert provider.calls == 2 and tool.executions == 1
        assert state.terminal_status == "completed"

    asyncio.run(scenario())


def test_completed_response_starts_a_fresh_retry_budget_for_the_next_tool_step(tmp_path):
    async def scenario():
        tool = WorkTool("perform_once", concurrent=False)

        class Provider(LLMAdapter):
            calls = 0

            async def stream_chat(self, messages, tools=None):
                self.calls += 1
                if self.calls <= 3:
                    yield committed(tool.name, f"call-{self.calls}")
                    if self.calls == 2:
                        yield StreamEvent(type=StreamEventType.DONE, finish_reason="tool_calls")
                    else:
                        yield StreamEvent(type=StreamEventType.ERROR, content="HTTP 503 service unavailable")
                else:
                    yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="Recovered both steps")
                    yield StreamEvent(type=StreamEventType.DONE)

            async def simple_chat(self, messages):
                return ""

        provider = Provider()
        state, _, _ = await run_case(tmp_path, provider, [tool], stream_max_attempts=1)
        assert provider.calls == 4 and tool.executions == 3
        assert state.terminal_status == "completed"

    asyncio.run(scenario())


@pytest.mark.parametrize("retries", [0, 1])
def test_actual_responses_http_failure_retains_tool_result_and_applies_retry_configuration(tmp_path, retries):
    import httpx
    from backend.config import LLMSettings
    from backend.llm.openai_adapter import OpenAIAdapter

    async def scenario():
        tool = WorkTool("perform_once", concurrent=False)
        requests = []
        handlers = set()

        async def serve(reader, writer):
            task = asyncio.current_task()
            handlers.add(task)
            try:
                header = (await reader.readuntil(b"\r\n\r\n")).decode()
                length = int(next(line.split(":", 1)[1] for line in header.splitlines()
                    if line.lower().startswith("content-length:")))
                body = json.loads(await reader.readexactly(length))
                requests.append(body)
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n")

                async def emit(value):
                    writer.write(f"event: {value['type']}\ndata: {json.dumps(value)}\n\n".encode())
                    await writer.drain()

                await emit({"type": "response.created", "response": {"id": f"response-{len(requests)}"}})
                if len(requests) == 1:
                    item = {"type": "function_call", "id": "fc-1", "call_id": "once", "name": tool.name,
                        "arguments": "{}", "status": "completed"}
                    await emit({"type": "response.output_item.added", "output_index": 0, "item": {**item, "arguments": "", "status": "in_progress"}})
                    await emit({"type": "response.output_item.done", "output_index": 0, "item": item})
                    await asyncio.wait_for(tool.finished.wait(), 5)
                    await emit({"type": "response.failed", "response": {"id": "response-1", "status": "failed",
                        "error": {"code": "server_error", "message": "HTTP 503 fixture failure after actual tool completion"}}})
                else:
                    item = {"id": "answer", "type": "message", "role": "assistant", "status": "completed",
                        "content": [{"type": "output_text", "text": "Recovered over HTTP", "annotations": []}]}
                    await emit({"type": "response.output_item.added", "output_index": 0, "item": {**item, "content": []}})
                    await emit({"type": "response.output_text.delta", "item_id": "answer", "output_index": 0,
                        "content_index": 0, "delta": "Recovered over HTTP"})
                    await emit({"type": "response.output_item.done", "output_index": 0, "item": item})
                    await emit({"type": "response.completed", "response": {"id": "response-2", "status": "completed", "output": [item],
                        "usage": {"input_tokens": 10, "output_tokens": 3, "total_tokens": 13}}})
            finally:
                writer.close()
                await writer.wait_closed()
                handlers.discard(task)

        server = await asyncio.start_server(serve, "127.0.0.1", 0)
        async with server, httpx.AsyncClient(trust_env=False) as client:
            port = server.sockets[0].getsockname()[1]
            adapter = OpenAIAdapter(LLMSettings(provider="custom", api_key="fixture", model="fixture-model",
                base_url=f"http://127.0.0.1:{port}/v1", wire_api="responses"), http_client=client)
            try:
                state, _, events = await run_case(tmp_path, adapter, [tool], stream_max_attempts=retries)
                assert len(requests) == retries + 1 and tool.executions == 1
                assert state.terminal_status == ("completed" if retries else "failed")
                assert any(event.type == "tool_result" and event.data["id"] == "once" for event in events)
                if retries:
                    outputs = [item for item in requests[1]["input"] if item.get("type") == "function_call_output"]
                    assert len(outputs) == 1 and outputs[0]["call_id"] == "once"
                    assert "perform_once finished" in outputs[0]["output"]
            finally:
                await adapter.aclose()
                await asyncio.gather(*handlers)

    asyncio.run(scenario())
