from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

from backend.agent.context import ContextBuilder
from backend.agent.execution_journal import ExecutionJournal
from backend.agent.loop_session import AgentLoopSessionContext
from backend.agent.model_execution import ModelExecutionSnapshot
from backend.agent.query_engine import AgentSession, QueryEngine, QuerySubmission
from backend.agent.run_context import RunContext
from backend.agent.runtime import AgentRuntime
from backend.agent.state import AgentState
from backend.artifact.store import ArtifactStore
from backend.config import AgentSettings, AppConfig, LLMSettings, PermissionSettings, TokenBudget
from backend.hooks.manager import HookEvent, HookManager, _HookEntry
from backend.llm.base import LLMAdapter, LLMMessage, StreamEvent, StreamEventType, UsageInfo, estimate_llm_context_tokens
from backend.llm.capabilities import ProviderCapabilities
from backend.llm.model_runtime import ModelRuntime
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext
from backend.services import chat_api_service as rest
from backend.services.llm_adapter_factory import build_provider_adapter
from backend.tools.registry import ToolRegistry

PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="


class Model(LLMAdapter):
    def __init__(self, mode="normal", model="audit-model", window=100000):
        self.mode, self._model, self._small_fast_model = mode, model, model
        self.capabilities = ProviderCapabilities(provider="offline", model=model, tool_calling=True, vision=True, context_window=window)
        self.main_inputs, self.summary_inputs = [], []
        self.side_started, self.release = asyncio.Event(), asyncio.Event()

    async def stream_chat(self, messages, tools=None, metadata=None):
        self.main_inputs.append(messages)
        if self.mode == "reactive" and len(self.main_inputs) == 1:
            yield StreamEvent(StreamEventType.ERROR, content="prompt too long", raw={"error_type": "prompt_too_long", "provider_error_type": "prompt_too_long"})
            return
        if self.mode == "commentary_failure":
            yield StreamEvent(StreamEventType.TEXT_CHUNK, content="Checking the project.", phase="commentary", item_id="commentary")
            yield StreamEvent(StreamEventType.ERROR, content="Invalid API key", raw={"provider_error_type": "auth", "error_type": "api"})
            return
        yield StreamEvent(StreamEventType.TEXT_CHUNK, content="First.", phase="final_answer", item_id="" if self.mode == "two_final" else "first", lifecycle="end" if self.mode == "two_final" else "delta")
        if self.mode == "two_final":
            yield StreamEvent(StreamEventType.TEXT_CHUNK, content="Second.", phase="final_answer", lifecycle="end")
        yield StreamEvent(StreamEventType.DONE, finish_reason="stop", usage=UsageInfo(input_tokens=30, output_tokens=4))

    async def simple_chat(self, messages, *, max_tokens=None):
        return "Summary: preserve A17 and the original constraints."

    async def _side_query_chat(self, messages, *, context, max_tokens=None):
        self.summary_inputs.append((context.options.operation, estimate_llm_context_tokens(messages)))
        self.side_started.set()
        if self.mode in {"slow_hook", "reactive"}:
            await self.release.wait()
        if context.options.operation.startswith("hook_prompt"):
            return '{"ok":true}'
        assert estimate_llm_context_tokens(messages) < self.capabilities.context_window
        return await self.simple_chat(messages, max_tokens=max_tokens)


class Turn:
    def __init__(self, root, llm, *, budget=None, settings=None, hook=None, owner=None):
        root.mkdir()
        self.runtime = AgentRuntime(metrics_file=root / "metrics.jsonl", swarm_store_dir=root / "swarm", enable_lease_heartbeat=False)
        self.owner = owner or RunContext()
        self.owner.agent_runtime = self.runtime
        self.owner.execution_journal = ExecutionJournal(root.name, base_dir=root / "journal")
        self.owner.hook_manager = hook
        self.cancel = asyncio.Event()
        self.state = AgentState(user_message="Preserve A17 and finish.", conversation_id=root.name)
        self.settings = settings or AgentSettings(max_iterations=6, code_mode_only=False, stream_max_attempts=0, compaction_keep_recent_tokens=128)
        self.budget = budget or TokenBudget(total=100000, response_reserve=1024)
        self.context = ContextBuilder(llm=llm, token_budget=self.budget, agent_settings=self.settings)
        self.context._get_project_guidelines = lambda *args, **kwargs: ""
        self.session = AgentSession(llm=llm, tool_registry=ToolRegistry(), artifact_store=ArtifactStore(storage_dir=root / "artifacts"),
            permission_checker=PermissionChecker(PermissionSettings()), agent_settings=self.settings, token_budget=self.budget, context_builder=self.context)
        self.submission = QuerySubmission(user_message=self.state.user_message, session=self.session, state=self.state,
            runtime=AgentLoopSessionContext(session_id=root.name, permission_context=PermissionContext(mode="bypass"),
                cancel_event=self.cancel, metadata={"conversation_id":root.name}, run_context=self.owner))
        self.events = []

    async def run(self):
        async for event in QueryEngine().submit(self.submission):
            self.events.append(event)

    async def close(self):
        await self.session.aclose()
        self.runtime.close(release_lease=True)


def seed(context, count=40):
    for index in range(count):
        context._history_store.append(LLMMessage(role="user", content=f"Preserve A17, prior turn {index}: " + "older work " * 200, is_user_input=True))
        context.append_assistant("complete " * 60)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,expected,status", [("two_final","First.Second.","completed"), ("commentary_failure","","failed")])
async def test_rest_returns_query_engine_canonical_answer(tmp_path, monkeypatch, mode, expected, status):
    llm = Model(mode)
    runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl", enable_lease_heartbeat=False)
    config = AppConfig(llm=LLMSettings(api_key="offline",model="audit-model"),agent=AgentSettings(max_iterations=5, code_mode_only=False, stream_max_attempts=0), token_budget=TokenBudget(total=100000, response_reserve=1024))
    monkeypatch.setattr(rest, "load_config", lambda **kwargs: config)
    monkeypatch.setattr(rest, "default_runtime", lambda: runtime)
    monkeypatch.setattr(rest, "ArtifactStore", lambda: ArtifactStore(storage_dir=tmp_path / "artifacts"))
    states = []
    class Engine(QueryEngine):
        async def submit(self, submission):
            states.append(submission.state)
            async for event in super().submit(submission):
                yield event
    bootstrap = SimpleNamespace(create_tool_registry=lambda *args, **kwargs: ToolRegistry(),
        create_permission_checker=lambda **kwargs: PermissionChecker(PermissionSettings()), create_llm=lambda **kwargs:llm, mcp_manager=None)
    result = await rest.run_rest_chat(message="Finish the task", max_iterations=None, bootstrap=bootstrap,
        query_engine=Engine(), conversation_id=tmp_path.name, permission_mode="bypass")
    assert result["reply"] == expected
    assert result["reply"] == states[0].reply
    assert result["status"] == status
    runtime.close(release_lease=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("api", ["openai-responses", "anthropic-messages"])
async def test_declared_text_input_and_parallel_policy_reach_context_and_wire(api):
    runtime = ModelRuntime(provider_configs={"owned": {"api":api, "base_url":"https://audit.invalid/v1", "api_key":"offline",
        "models":[{"id":"text-model","input":["text"],"parallel_tool_calls":False,"native_compaction":False,"context_window":100000}]}})
    adapter = build_provider_adapter("owned", "text-model", model_runtime=runtime)
    assert adapter.capabilities.vision is False
    assert adapter.capabilities.parallel_tool_calls is False
    ctx = ContextBuilder(llm=adapter, token_budget=TokenBudget(total=100000, response_reserve=1024))
    ctx._get_project_guidelines = lambda *args, **kwargs: ""
    ctx._history_store.append(LLMMessage("user", "Read the attachment", images=[{"data":PNG,"media_type":"image/png"}], is_user_input=True))
    messages = await ctx.build(AgentState(user_message="Read the attachment"))
    assert not any(message.images for message in messages)
    captured = []
    async def respond(request):
        captured.append(json.loads(request.content))
        if api == "openai-responses":
            events = [{"type":"response.completed","response":{"id":"r","status":"completed","output":[],"usage":{"input_tokens":1,"output_tokens":0}}}]
        else:
            events = [{"type":"message_start","message":{"id":"m","role":"assistant","model":"text-model","content":[],"usage":{"input_tokens":1,"output_tokens":0}}},
                {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":0}}, {"type":"message_stop"}]
        return httpx.Response(200, headers={"content-type":"text/event-stream"}, text="".join("data: "+json.dumps(event)+"\n\n" for event in events))
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter._http_client = client
        tools = [{"type":"function", "function":{"name":"read_file","description":"read","parameters":{"type":"object","properties":{}}}}]
        events = [event async for event in adapter.stream_chat(messages, tools)]
        assert any(event.type == StreamEventType.DONE for event in events)
    assert "input_image" not in json.dumps(captured[0]) and '"type": "image"' not in json.dumps(captured[0])
    if api == "openai-responses":
        assert captured[0]["parallel_tool_calls"] is False
    else:
        assert captured[0]["tool_choice"] == {"type":"auto", "disable_parallel_tool_use":True}
    await adapter.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("first_phase", ["commentary", "final_answer"])
async def test_responses_end_turn_false_replays_native_items_and_only_finalizes_second_response(tmp_path, first_phase):
    from backend.llm.openai_adapter import OpenAIAdapter
    first_items = [{"type":"reasoning","id":"reason-one","encrypted_content":"opaque-reasoning","summary":[]},
        {"type":"message","id":"first-item","role":"assistant","phase":first_phase,"content":[{"type":"output_text","text":"Intermediate.","annotations":[]}]}]
    requests = []
    async def respond(request):
        requests.append(json.loads(request.content))
        second = len(requests) == 2
        output = [{"type":"message","id":"second-item","role":"assistant","phase":"final_answer","content":[{"type":"output_text","text":"Accepted result.","annotations":[]}]}] if second else first_items
        event = {"type":"response.completed","response":{"id":f"r-{len(requests)}","status":"completed","end_turn":second,"output":output,"usage":{"input_tokens":20,"output_tokens":3}}}
        return httpx.Response(200, headers={"content-type":"text/event-stream"}, text="data: "+json.dumps(event)+"\n\n")
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = OpenAIAdapter(LLMSettings(provider="custom",api_key="offline",model="native-model",base_url="https://audit.invalid/v1",wire_api="responses",proxy_mode="direct",native_compaction=False),http_client=client)
        turn = Turn(tmp_path / "native-follow-up", adapter)
        await turn.run()
        assert len(requests) == 2
        assert turn.state.reply == "Accepted result."
        assert [event.data["status"] for event in turn.events if event.type == "done"] == ["completed"]
        assert [item for item in requests[1]["input"] if item.get("id") in {"reason-one", "first-item"}] == first_items
        assert any(message.provider_items for message in turn.context._history)
        assert not any(message.role == "user" and "Continue" in message.content for message in turn.context._history)
        assert turn.state.total_retries == 0
        if first_phase == "final_answer":
            updates = [event.data["item"] for event in turn.events if event.type == "item.completed" and event.data["item"].get("id") == "first-item"]
            assert updates[-1]["source"] == "commentary"
        await turn.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["deadline", "cancel_event"])
async def test_stop_hook_uses_turn_deadline_and_event_cancellation(tmp_path, boundary):
    import re
    llm = Model("slow_hook")
    hook = HookManager(hooks={HookEvent.STOP:[_HookEntry(matcher=re.compile(""),hook_type="prompt",prompt="Approve the answer.",async_timeout=2)]})
    settings = AgentSettings(max_iterations=3,code_mode_only=False,stream_max_attempts=0,max_turn_seconds=0)
    turn = Turn(tmp_path / "stop-boundary", llm, settings=settings, hook=hook)
    if boundary == "deadline":
        import time
        stream = llm.stream_chat

        async def stream_with_stop_deadline(messages, tools=None, metadata=None):
            async for event in stream(messages, tools, metadata):
                if event.type == StreamEventType.DONE:
                    # This fixture targets Stop's auxiliary wait; filesystem
                    # startup/checkpoints must not consume its 80 ms deadline.
                    turn.submission.runtime.deadline_controller.turn_deadline = time.monotonic() + .08
                yield event

        llm.stream_chat = stream_with_stop_deadline
    task = asyncio.create_task(turn.run())
    await asyncio.wait_for(llm.side_started.wait(), 2)
    if boundary == "cancel_event":
        turn.cancel.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
    else:
        await asyncio.wait_for(task, 1)
    assert turn.state.reply == ("First." if boundary == "cancel_event" else "")
    assert not any(event.type == "done" and event.data["status"] == "completed" for event in turn.events)
    assert turn.owner.llm_turn_context.side_call_records[-1]["status"] == "cancelled"
    await turn.close()


@pytest.mark.asyncio
async def test_reactive_compaction_cancel_does_not_install_history(tmp_path):
    llm = Model("reactive")
    turn = Turn(tmp_path / "reactive-boundary", llm)
    seed(turn.context)
    task = asyncio.create_task(turn.run())
    await asyncio.wait_for(llm.side_started.wait(), 2)
    turn.cancel.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert turn.context._compaction_count == 0
    assert turn.owner.llm_turn_context.side_call_records[-1]["status"] == "cancelled"
    assert not llm.release.is_set()
    await turn.close()


@pytest.mark.asyncio
async def test_downshift_summarizes_with_previous_model_before_smaller_model_runs(tmp_path):
    old = Model(model="large-model",window=100000)
    new = Model(model="small-model",window=20000)
    settings = AgentSettings(max_iterations=3,code_mode_only=False,stream_max_attempts=0,compaction_keep_recent_tokens=128)
    config = AppConfig(llm=LLMSettings(api_key="offline",model="small-model"),agent=settings,token_budget=TokenBudget(total=20000,response_reserve=1024))
    owner = RunContext(model_execution=ModelExecutionSnapshot(config,new,"offline","small-model"))
    turn = Turn(tmp_path / "downshift", new, owner=owner, settings=settings)
    turn.context.bind_llm(old)
    seed(turn.context, 80)
    await turn.run()
    assert old.summary_inputs and not new.summary_inputs
    assert old.summary_inputs[0][1] > 20000
    assert len(new.main_inputs) == 1
    assert estimate_llm_context_tokens(new.main_inputs[0]) < 20000
    assert turn.state.reply == "First."
    assert turn.context._llm is new and turn.context._budget.total == 20000
    assert any(event.type == "context_compacted" for event in turn.events)
    await turn.close()
