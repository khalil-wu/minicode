from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from backend.agent.context import CompactionNoopError
from backend.agent.lifecycle_errors import LifecycleStaleError
from backend.agent.max_output_recovery import recover_max_output
from backend.agent.provider_attempt import ProviderAttempt
from backend.agent.provider_completion import ProviderCompletionCoordinator
from backend.agent.provider_response_recovery import recover_provider_response, PostStreamRecoveryResult
from backend.agent.provider_stream_control import reset_for_provider_retry
from backend.agent.provider_stream_runtime import stream_provider_response
from backend.agent.provider_stream_settlement import record_provider_attempt_usage
from backend.agent.provider_text_projection import project_provider_text_chunk
from backend.agent.provider_event_projection import project_non_text_provider_event
from backend.agent.state import AgentState
from backend.agent.stream_attempt import StreamAttemptState, StreamTextState
from backend.agent.stream_sanitizer import ThinkingStreamSanitizer
from backend.agent.tool_stream_tracker import StreamingToolTracker
from backend.config import LLMSettings
from backend.llm.base import LLMAdapter, LLMMessage, StreamEvent, StreamEventType, ToolCallEvent, UsageInfo
from backend.llm.cost_tracker import CostTracker
from backend.llm.model_runtime import ModelRuntime
from backend.llm.openai_adapter import OpenAIAdapter
from backend.services import llm_adapter_factory as factory
from backend.tools.registry import ToolRegistry


class _Kernel:
    metadata = {}
    run_record = SimpleNamespace(run_id="contract-run")
    next_provider_call_count = 1

    def __init__(self):
        self.closed = []

    async def start_provider_attempt(self, **kwargs):
        return ProviderAttempt(kwargs["iteration_id"], kwargs["retry_index"], "span", kwargs["started_at"])

    async def observe_provider_first_event(self, *args, **kwargs):
        return None

    async def close_provider_attempt(self, attempt, **kwargs):
        if attempt is not None and not attempt.closed:
            attempt.closed = True
            self.closed.append(kwargs["status"])

    def pop_turn_steer(self):
        return None


@pytest.fixture
def tracker(monkeypatch):
    tracker = CostTracker()
    monkeypatch.setattr(CostTracker, "_instance", tracker)
    return tracker


@pytest.mark.asyncio
async def test_actual_wire_model_owns_price_even_when_error_metadata_is_sparse(tracker):
    class Hooks:
        async def emit_before_provider_request(self, payload):
            return {**payload, "model": "small-model"}
        async def emit_before_provider_headers(self, headers):
            return headers
        async def emit_after_provider_response(self, status, headers):
            return None

    async def response(request):
        assert json.loads(request.content)["model"] == "small-model"
        return httpx.Response(200, content='data: {"choices":[],"usage":{"prompt_tokens":100,"completion_tokens":10}}\n\ndata: {"error":{"message":"unauthorized","type":"authentication_error","code":"invalid_api_key"}}\n\n')

    async with httpx.AsyncClient(transport=httpx.MockTransport(response), trust_env=False) as client:
        adapter = OpenAIAdapter(LLMSettings(api_key="placeholder", model="main-model", base_url="https://contract.invalid/v1", wire_api="chat"), http_client=client)
        adapter._request_model_costs = {"main-model": {"input": 10, "output": 20, "cacheRead": 0, "cacheWrite": 0}, "small-model": {"input": 1, "output": 2, "cacheRead": 0, "cacheWrite": 0}}
        events = [event async for event in adapter.stream_chat([LLMMessage("user", "contract")], metadata={"_lifecycle_runtime": Hooks()})]
        usage = next(event for event in events if event.type is StreamEventType.USAGE)
        error = events[-1]
        assert error.type is StreamEventType.ERROR
        stream = StreamAttemptState(usage=usage.usage, raw_done=dict(usage.raw))
        attempt = ProviderAttempt("i1", 0, "span", 0, usage_reported=True)
        turn = UsageInfo(cost_usd=0)
        arguments = dict(llm=adapter, provider_attempt=attempt, stream_state=stream, provider_raw_done=stream.raw_done, budget_runtime=SimpleNamespace(cost_session_id="contract", record_provider_usage_total=lambda usage: None), turn_usage=turn, chain=SimpleNamespace(record_usage=lambda **kwargs: None), raw=error.raw)
        record_provider_attempt_usage(**arguments)
        record_provider_attempt_usage(**arguments)
    assert error.raw["model"] == "small-model"
    assert turn.cost_usd == pytest.approx(.00012)
    assert turn.input_tokens == 100 and turn.output_tokens == 10
    assert tracker.get_summary("contract")["priced_requests"] == 1


def test_sparse_done_keeps_same_attempt_metadata_and_terminal_overrides():
    stream = StreamAttemptState(response_phase="final_answer", raw_done={"model": "wire-model", "request_summary": {"model": "wire-model"}, "usage": {"input_tokens": 10}, "response_id": "old-id"})
    stream.accept_provider_event(StreamEvent(StreamEventType.DONE, usage=UsageInfo(input_tokens=10), raw={"response_id": "terminal-id"}))
    assert stream.raw_done["model"] == "wire-model"
    assert stream.raw_done["request_summary"]["model"] == "wire-model"
    assert stream.raw_done["response_id"] == "terminal-id"
    assert stream.response_phase == "final_answer"
    stream.reset_provider_payload()
    assert stream.raw_done == {} and stream.response_phase == ""


def test_model_mirror_and_later_model_changes_cannot_reprice_captured_step(monkeypatch):
    cost = {"input": 1, "output": 2, "cacheRead": 0, "cacheWrite": 0, "tiers": [{"inputTokensAbove": 100, "input": 3, "output": 4, "cacheRead": 0, "cacheWrite": 0}]}
    runtime = ModelRuntime(provider_configs={"owned": {"api": "openai-completions", "base_url": "https://contract.invalid/v1", "api_key": "placeholder", "models": [{"id": "model", "cost": cost}]}})
    monkeypatch.setattr(factory, "_build_registered_provider_adapter", lambda spec: SimpleNamespace())
    adapter = factory.build_provider_adapter("owned", "model", model_runtime=runtime)
    model = runtime.get_model("owned", "model")
    for mirror in (model.to_extension_dict(), model.to_public_dict()):
        mirror["cost"]["tiers"][0]["input"] = 9
    assert model.cost["tiers"][0]["input"] == 3
    model.cost["tiers"][0]["input"] = 11
    assert adapter._request_model_costs["model"]["tiers"][0]["input"] == 3


@pytest.mark.asyncio
async def test_retry_closes_only_queued_calls_before_forgetting_payload():
    queued = ToolCallEvent(id="queued", name="read", arguments={})
    yielded = ToolCallEvent(id="yielded", name="read", arguments={})
    stream = StreamAttemptState(tool_calls=[queued, yielded], partial_tool_names={"queued": "read", "yielded": "read"})
    tools = StreamingToolTracker()
    tools.add_tools([queued, yielded])
    tools.mark_yielded(yielded.id)
    events = [event async for event in reset_for_provider_retry(stream_text=StreamTextState(iteration_id="i1"), stream_state=stream, tool_tracker=tools)]
    results = [event for event in events if getattr(event, "type", "") == "tool_result"]
    assert [event.data["id"] for event in results] == [queued.id]
    assert results[0].data["error_kind"] == "provider_retry_before_tool_execution"
    assert stream.tool_calls == []


@pytest.mark.asyncio
async def test_truncation_recovery_closes_complete_unexecuted_tool():
    call = ToolCallEvent(id="queued", name="read", arguments={"path": "contract.txt"})
    stream = StreamAttemptState(tool_calls=[call], partial_tool_names={call.id: call.name}, final_tool_batch_received=True)
    tools = StreamingToolTracker()
    tools.add_tools([call])
    events = [event async for event in recover_provider_response(state=AgentState(user_message="contract"), stream_state=stream, stream_text=StreamTextState(iteration_id="i1", pending_unphased_text="partial", pending_unphased_visible_text="partial"), tool_tracker=tools, context_builder=SimpleNamespace(append_assistant=lambda *args, **kwargs: None, append_user=lambda *args: None, hook_manager=None), budget_runtime=SimpleNamespace(consume_retry=lambda reason: None), turn_usage=UsageInfo(), finish_reason="length", scrub_text=lambda value: value, tool_batch_count=0, degraded_reason="")]
    results = [event for event in events if getattr(event, "type", "") == "tool_result"]
    assert len(results) == 1 and results[0].data["id"] == call.id
    assert results[0].data["error_kind"] == "provider_truncated_before_tool_execution"
    assert isinstance(events[-1], PostStreamRecoveryResult) and events[-1].action == "retry"


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["final_answer", "commentary"])
async def test_phase_only_part_completion_reclassifies_owned_text_without_empty_delta(phase):
    stream, text = StreamAttemptState(), StreamTextState(iteration_id="i1")
    sanitizer = ThinkingStreamSanitizer()
    arguments = dict(stream_state=stream, stream_text=text, visible_text_sanitizer=sanitizer, provider_raw_final_text=stream.raw_final_text, live_text_streaming=True, awaiting_trailing_done=False, process_event_factory=lambda *args, **kwargs: None)
    _ = [event async for event in project_provider_text_chunk(StreamEvent(StreamEventType.TEXT_CHUNK, content="body", item_id="message"), **arguments)]
    phase_updates = [event async for event in project_provider_text_chunk(StreamEvent(StreamEventType.TEXT_CHUNK, item_id="message", phase=phase, lifecycle="end"), **arguments)]
    assert all(getattr(event, "type", "") != "agent_message.delta" for event in phase_updates)
    assert not stream.provider_done
    tools = StreamingToolTracker()
    call = ToolCallEvent(id="call", name="read", arguments={})
    updates = [event async for event in project_non_text_provider_event(StreamEvent(StreamEventType.TOOL_CALL, tool_calls=[call], tool_calls_committed=True), stream_state=stream, stream_text=text, live_text_streaming=True, tool_tracker=tools, tool_registry=ToolRegistry(), tool_context=SimpleNamespace(), process_event_factory=lambda *args, **kwargs: None)]
    completed = next(event.data["item"] for event in updates if getattr(event, "type", "") == "item.completed")
    assert completed["source"] == ("model_final" if phase == "final_answer" else "commentary")
    assert text.final_candidate_text == ("body" if phase == "final_answer" else "")


@pytest.mark.asyncio
async def test_eof_is_failed_attempt_while_consumer_close_is_cancelled(tracker):
    class Adapter(LLMAdapter):
        async def stream_chat(self, messages, tools=None, metadata=None):
            yield StreamEvent(StreamEventType.TEXT_CHUNK, content="partial")
        async def simple_chat(self, messages, **kwargs):
            return "unused"

    async def unused(*args, **kwargs):
        if False:
            yield None

    def create(kernel, state):
        settings = SimpleNamespace(stream_max_attempts=0, live_text_streaming=True, stream_timeout_seconds=1)
        return stream_provider_response(llm=Adapter(), messages=[], tool_schemas=[], llm_request_metadata={}, prompt_cache_safe_params={}, provider_completion=ProviderCompletionCoordinator(settings=settings, state=state, turn_kernel=kernel, prompt_cache_tracking_source="contract", turn_started_at=0, turn_start_tool_call_count=0), state=state, context_builder=SimpleNamespace(), turn_kernel=kernel, budget_runtime=SimpleNamespace(ensure_started=lambda: None, cost_session_id="contract", bounded_provider_timeout=lambda value: (value, False), record_provider_usage_total=lambda usage: None), turn_usage=UsageInfo(cost_usd=0), settings=settings, tool_registry=ToolRegistry(), permission_checker=None, effective_permission_context=None, tool_context=SimpleNamespace(cancel_event=asyncio.Event(), pending_provider_tasks=set(), run_context=SimpleNamespace(refresh_model_auth=None), model_execution=None), turn_start_tool_call_count=0, turn_started_at=0, iteration_limit=10, tool_batch_count=0, iteration_id_value="i1", stream_retry_policy=None, error_controller=None, chain=SimpleNamespace(record_usage=lambda **kwargs: None), stream_text=StreamTextState(iteration_id="i1"), degrade_and_finish=unused, recover_withheld_error=None)

    kernel, state = _Kernel(), AgentState(user_message="contract")
    _ = [event async for event in create(kernel, state)]
    assert kernel.closed == ["failed"] and state.stopped_reason == "provider_terminal_missing"
    kernel, state = _Kernel(), AgentState(user_message="contract")
    events = create(kernel, state)
    await anext(events)
    await events.aclose()
    assert kernel.closed == ["cancelled"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [LifecycleStaleError("retired owner"), RuntimeError("internal compactor failed")])
async def test_emergency_compaction_does_not_hide_internal_owner_failure(failure):
    async def compact(**kwargs):
        raise failure
    async def stop_hook(*args, **kwargs):
        return None
    with pytest.raises(type(failure)):
        await recover_max_output(state=AgentState(user_message="contract"), stream_text=StreamTextState(), tool_tracker=StreamingToolTracker(), context_builder=SimpleNamespace(full_compact=compact), budget_runtime=SimpleNamespace(consume_retry=lambda reason: None), provider_items=[], turn_usage=UsageInfo(), finish_reason="model_context_window_exceeded", scrub_text=lambda value: value, run_stop_failure_hook=stop_hook)


@pytest.mark.asyncio
async def test_expected_compaction_noop_keeps_context_window_failure():
    async def compact(**kwargs):
        raise CompactionNoopError()
    async def stop_hook(*args, **kwargs):
        return None
    result = await recover_max_output(state=AgentState(user_message="contract"), stream_text=StreamTextState(), tool_tracker=StreamingToolTracker(), context_builder=SimpleNamespace(full_compact=compact), budget_runtime=SimpleNamespace(consume_retry=lambda reason: None), provider_items=[], turn_usage=UsageInfo(), finish_reason="model_context_window_exceeded", scrub_text=lambda value: value, run_stop_failure_hook=stop_hook)
    assert result.action == "terminate"
