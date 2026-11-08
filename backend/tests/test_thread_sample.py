from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import httpx
import json
import pytest

from backend.agent.context import ContextBuilder
from backend.agent.loop_session import AgentLoopSessionContext
from backend.agent.model_execution import ModelExecutionSnapshot
from backend.agent.query_engine import AgentSession, QuerySubmission
from backend.agent.run_context import RunContext
from backend.agent.runtime import AgentRuntime
from backend.agent.state import AgentState
from backend.agent.thread_sample import SampleCapture, SampleError, SampleParams, collect_sample, sample_turn
from backend.artifact.store import ArtifactStore
from backend.config import AppConfig, AgentSettings, LLMSettings, PermissionSettings, TokenBudget
from backend.llm.base import LLMAdapter, LLMMessage, StreamEvent, StreamEventType as E, ToolCallEvent, UsageInfo, capture_response_items
from backend.llm.openai_adapter import OpenAIAdapter
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext
from backend.tests.test_code_execution import FixtureTool
from backend.tools.registry import ToolRegistry
from backend.ws.thread_sample import SampleRequest, ThreadSampleRequests, sample_model_config


async def stream(events):
    for event in events:
        yield event


def done(**kwargs):
    return StreamEvent(E.DONE, response_id="actual-response", **kwargs)


@pytest.mark.asyncio
async def test_final_items_replace_deltas_and_ignore_commentary_and_reasoning():
    result = await collect_sample(stream([
        StreamEvent(E.THINKING_CHUNK, "private reasoning"),
        StreamEvent(E.TEXT_CHUNK, "ignored", phase="commentary", item_id="c"),
        StreamEvent(E.TEXT_CHUNK, "hel", item_id="a"),
        StreamEvent(E.TRANSPORT_ACTIVITY, provider_items=[{"type": "message", "role": "assistant", "id": "a",
            "content": [{"type": "output_text", "text": "hello"}]}]),
        done(usage=UsageInfo(input_tokens=30, cache_read_input_tokens=20, output_tokens=2)),
    ]))
    assert result == {"responseId": "actual-response", "text": "hello", "inputTokens": 30,
        "cachedInputTokens": 20, "outputTokens": 2}


@pytest.mark.asyncio
@pytest.mark.parametrize("event", [StreamEvent(E.TOOL_CALL_START), StreamEvent(E.TOOL_CALL),
    StreamEvent(E.IMAGE_CHUNK), StreamEvent(E.PROVIDER_ACTIVITY),
    StreamEvent(E.TRANSPORT_ACTIVITY, provider_items=[{"type": "local_shell_call"}]),
    StreamEvent(E.TEXT_CHUNK, "refused", content_kind="refusal")])
async def test_nontext_attempt_returns_null_with_missing_usage(event):
    result = await collect_sample(stream([StreamEvent(E.TEXT_CHUNK, "text"), event, done()]))
    assert result["text"] is None
    assert result["inputTokens"] is result["cachedInputTokens"] is result["outputTokens"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("events,match", [([StreamEvent(E.TEXT_CHUNK, "uncommitted")], "before completion"),
    ([StreamEvent(E.ERROR, "model error")], "model error"),
    ([StreamEvent(E.TEXT_CHUNK, "中" * 22000), done()], "exceeds limit")])
async def test_sample_errors_are_terminal(events, match):
    with pytest.raises(SampleError, match=match):
        await collect_sample(stream(events))


@pytest.mark.parametrize("change", [{"prompt": " "}, {"prompt": "中" * 1334}, {"expectedTurnId": ""},
    {"disableMcp": 1}, {"responsesapiClientMetadata": {"x": 3}}, {"effort": "invalid"}, {"threadSource": ""}])
def test_source_prompt_contract(change):
    with pytest.raises(SampleError):
        SampleParams.from_wire({"threadId": "thread", "expectedTurnId": "turn", "prompt": "ok", **change})


@pytest.mark.parametrize("provider,model,expected", [("openai", "gpt-6.1-sol", "high"),
    ("custom", "gpt-6.1-sol", "low"), ("openai", "unknown", "low")])
def test_effort_override_preserves_source_provider_and_model(provider, model, expected):
    config = AppConfig(llm=LLMSettings(provider=provider, model=model, reasoning_effort="low", api_key="test"))
    selected, _, _ = sample_model_config(SimpleNamespace(config=config, model_selection={}), None,
        SampleParams("thread", "turn", "prompt", effort="high"))
    assert selected.llm.reasoning_effort == expected
    assert selected.llm.model == model and config.llm.reasoning_effort == "low"


class Model(LLMAdapter):
    def __init__(self, events=(), *, hold=False):
        self._settings = LLMSettings(provider="custom", model="sample-fixture", wire_api="responses", api_key="test")
        self.events, self.hold = events, hold
        self.started, self.closed = asyncio.Event(), asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0
        self.inputs = []
        self.schemas = []

    async def simple_chat(self, *args, **kwargs):
        raise AssertionError("sample must not use side query or compaction")

    async def stream_chat(self, messages, tools=None, metadata=None):
        self.calls += 1
        self.inputs = list(messages)
        self.schemas = tools
        self.started.set()
        try:
            if self.hold:
                await self.release.wait()
            for event in self.events:
                yield event
        finally:
            self.closed.set()

    async def aclose(self):
        self.closed.set()


def submission(tmp_path, model):
    tool = FixtureTool("sample_write", effect=True)
    registry = ToolRegistry()
    registry.register(tool)
    budget = TokenBudget(total=12000, response_reserve=1000)
    settings = AgentSettings(code_mode_only=False, max_turn_seconds=0, stream_max_attempts=3)
    config = AppConfig(llm=model._settings, agent=settings, token_budget=budget)
    builder = ContextBuilder(llm=model, token_budget=budget, conversation_id="sample", workspace_root=tmp_path)
    builder._history = [LLMMessage("user", "original user"), LLMMessage("assistant", "original answer")]
    builder.auto_compaction_disabled = True
    runtime = AgentRuntime(metrics_file=tmp_path / "runtime/metrics.jsonl", swarm_store_dir=tmp_path / "runtime/swarm")
    capture = SampleCapture()
    run = RunContext(agent_runtime=runtime, response_capture=capture, cancel_event=asyncio.Event(),
        model_execution=ModelExecutionSnapshot.capture(config, model))
    agent = AgentSession(llm=model, tool_registry=registry, artifact_store=ArtifactStore(storage_dir=tmp_path / "artifacts"),
        owns_artifact_store=True, permission_checker=PermissionChecker(PermissionSettings(), tmp_path),
        agent_settings=settings, token_budget=budget, context_builder=builder, approval_handler=capture.reject_interaction)
    return QuerySubmission(user_message="sample prompt", session=agent,
        state=AgentState(user_message="sample prompt", conversation_id="sample", workspace_root=tmp_path),
        runtime=AgentLoopSessionContext(workspace_root=tmp_path, session_id="sample", run_context=run,
            cancel_event=run.cancel_event, permission_context=PermissionContext(mode="bypass"),
            emit_event=capture.observe, metadata={"conversation_id": "sample"})), runtime, tool


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["text", "tool", "error", "eof"])
async def test_canonical_query_captures_once_without_tools_recovery_or_stop_hooks(tmp_path, monkeypatch, kind):
    events = {"text": [StreamEvent(E.TEXT_CHUNK, "sampled answer"), done()],
        "tool": [StreamEvent(E.TOOL_CALL, tool_calls=[ToolCallEvent("t", "sample_write", {})], tool_calls_committed=True), done()],
        "error": [StreamEvent(E.ERROR, "fixture failed")], "eof": []}[kind]
    model = Model(events)
    query, runtime, tool = submission(tmp_path, model)
    stop_calls = []
    async def no_stop(*args, **kwargs):
        stop_calls.append(True)
        raise AssertionError("sample must not run Stop hooks")
    monkeypatch.setattr("backend.hooks.manager.HookManager.run_stop", no_stop)
    try:
        operation = sample_turn(query, cancel=asyncio.Event(), deadline=asyncio.get_running_loop().time() + 10)
        if kind in {"error", "eof"}:
            with pytest.raises(SampleError):
                await operation
        else:
            result = await operation
            assert result["text"] == ("sampled answer" if kind == "text" else None)
        assert model.calls == 1 and model.closed.is_set()
        assert tool.executions == 0 and not (tmp_path / "marker.txt").exists()
        assert any(schema["function"]["name"] == "sample_write" for schema in model.schemas)
        assert any("original answer" in message.content for message in model.inputs)
        assert not stop_calls
    finally:
        await query.session.aclose()
        await runtime.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["cancel", "timeout"])
async def test_cancel_and_timeout_drain_the_actual_provider(tmp_path, reason):
    model = Model(hold=True)
    query, runtime, _ = submission(tmp_path, model)
    cancel = asyncio.Event()
    task = asyncio.create_task(sample_turn(query, cancel=cancel,
        deadline=asyncio.get_running_loop().time() + (0.8 if reason == "timeout" else 10)))
    try:
        await asyncio.wait_for(model.started.wait(), 5)
        if reason == "cancel":
            cancel.set()
        with pytest.raises(SampleError, match="canceled" if reason == "cancel" else "timed out"):
            await task
        assert model.closed.is_set() and not query.session.active_turn
        assert not [task for task in query.session.lifecycle_cleanup_tasks if not task.done()]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await query.session.aclose()
        await runtime.aclose()


@pytest.mark.asyncio
async def test_timeout_during_query_startup_releases_session_and_run(tmp_path, monkeypatch):
    model = Model([done()])
    query, runtime, _ = submission(tmp_path, model)
    entered = asyncio.Event()
    async def hold_startup(_context):
        entered.set()
        await asyncio.Future()
    monkeypatch.setattr("backend.agent.query_engine.QueryEngine._publish_system_prompt", staticmethod(hold_startup))
    try:
        with pytest.raises(SampleError, match="timed out"):
            await sample_turn(query, cancel=asyncio.Event(), deadline=asyncio.get_running_loop().time() + .3)
        assert entered.is_set() and not query.session.active_turn and model.calls == 0
        assert not query.runtime.run_context.lifecycle_cleanup_tasks
    finally:
        await query.session.aclose()
        await runtime.aclose()


@pytest.mark.asyncio
async def test_early_windows_timer_wakeup_cannot_turn_timeout_into_an_unbounded_wait(tmp_path, monkeypatch):
    model = Model(hold=True)
    query, runtime, _ = submission(tmp_path, model)
    real_wait = asyncio.wait
    async def early_timer(tasks, **kwargs):
        if query.runtime.run_context.response_capture.result in tasks:
            await asyncio.sleep(0)
            return set(), set(tasks)
        return await real_wait(tasks, **kwargs)
    monkeypatch.setattr(asyncio, "wait", early_timer)
    try:
        with pytest.raises(SampleError, match="timed out"):
            await asyncio.wait_for(sample_turn(query, cancel=asyncio.Event(),
                deadline=asyncio.get_running_loop().time() + 30), 5)
        assert not query.session.active_turn
    finally:
        await query.session.aclose()
        await runtime.aclose()


@pytest.mark.asyncio
async def test_cancel_request_identity_includes_connection_and_json_type():
    owner = ThreadSampleRequests(SimpleNamespace())
    cancel = asyncio.Event()
    owner.requests[(1, int, 7)] = SampleRequest(cancel, asyncio.current_task())
    for identity, generation in [("7", 1), (7, 2), (8, 1)]:
        owner.cancel(identity, generation)
        assert not cancel.is_set()
    owner.cancel(7, 1)
    owner.cancel(7, 1)
    assert cancel.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("item", [
    {"type": "message", "id": "answer", "role": "assistant", "content": [{"type": "output_text", "text": "native answer"}]},
    {"type": "local_shell_call", "id": "shell", "action": {"type": "exec", "command": ["never-run"]}},
    {"type": "custom_tool_call", "id": "custom", "name": "unknown-tool", "input": "never-run"},
])
async def test_actual_responses_adapter_captures_native_items_before_tool_decode(item):
    wire = [{"type": "response.output_item.added", "item": item}, {"type": "response.output_item.done", "item": item},
        {"type": "response.completed", "response": {"id": "provider-response", "status": "completed", "output": [item],
            "usage": {"input_tokens": 15, "output_tokens": 3, "input_tokens_details": {"cached_tokens": 10}}}}]
    async def respond(request):
        assert "capture_response_items" not in request.content.decode()
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
            content="".join("data: " + json.dumps(event) + "\n\n" for event in wire).encode(), request=request)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = OpenAIAdapter(LLMSettings(provider="custom", model="sample-fixture", base_url="https://example.test/v1",
            api_key="test", wire_api="responses", proxy_mode="direct"), http_client=client)
        token = capture_response_items.set(True)
        try:
            result = await collect_sample(adapter.stream_chat([LLMMessage("user", "sample")]))
            assert result == {"responseId": "provider-response", "text": "native answer" if item["type"] == "message" else None,
                "inputTokens": 15, "cachedInputTokens": 10, "outputTokens": 3}
        finally:
            capture_response_items.reset(token)
            await adapter.aclose()
