from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from backend.agent.provider_attempt import ProviderAttempt
from backend.agent.provider_completion import ProviderCompletionResult
from backend.agent.provider_event_projection import project_non_text_provider_event
from backend.agent.provider_protocol import add_usage
from backend.agent.provider_stream_error_event import ProviderErrorEventResult
from backend.agent.provider_stream_failures import ProviderStreamExceptionResult, recover_stream_timeout
from backend.agent.provider_stream_runtime import ProviderStreamResult, stream_provider_response
from backend.agent.provider_stream_wait import ProviderWaitResult
from backend.agent.rollout_budget import billable_tokens_from_usage
from backend.agent.stream_attempt import StreamAttemptState, StreamTextState
from backend.config import LLMSettings
from backend.llm.anthropic_adapter import AnthropicAdapter
from backend.llm.anthropic_protocol import _anthropic_replay_content
from backend.llm.base import LLMAdapter, LLMMessage, LLMSideCallContext, LLMTurnContext, SideQueryOptions, StreamEvent, StreamEventType, UsageInfo
from backend.llm.cost_tracker import CostTracker, estimate_usage_cost_usd
from backend.llm.model_runtime import ModelRuntime
from backend.llm.openai_adapter import OpenAIAdapter, _json_to_namespace, _responses_tool_calls_from_provider_items
from backend.llm.openai_usage import _get_usage_cost_usd


@pytest.fixture
def tracker(monkeypatch):
    value = CostTracker()
    monkeypatch.setattr(CostTracker, "_instance", value)
    return value


def test_cache_accounting_has_one_prompt_convention():
    chat = UsageInfo(input_tokens=1000, output_tokens=100, cache_creation_input_tokens=200, cache_read_input_tokens=700)
    messages = UsageInfo(input_tokens=50, output_tokens=10, cache_creation_input_tokens=300,
                         cache_read_input_tokens=900, input_includes_cache_read=False, input_includes_cache_write=False)
    total = add_usage(add_usage(UsageInfo(cost_usd=0), chat), messages)
    assert messages.input_tokens == 1250
    assert chat.billable_tokens == billable_tokens_from_usage(chat) == 400
    assert messages.billable_tokens == billable_tokens_from_usage(messages) == 360
    assert total.input_tokens == 2250
    assert total.ordinary_input_tokens == 150
    assert total.billable_tokens == billable_tokens_from_usage(total) == 760


def test_price_normalization_distinguishes_zero_unknown_and_cached_input(tracker):
    usage = UsageInfo(input_tokens=1000, output_tokens=100, cache_read_input_tokens=800)
    assert estimate_usage_cost_usd("claude-sonnet-4-6", usage) == pytest.approx(.00234)
    assert tracker.record_usage_info(usage, model_id="claude-sonnet-4-6") == "model_catalog"
    assert usage.cost_usd == pytest.approx(.00234)
    assert _get_usage_cost_usd({}) is None
    assert _get_usage_cost_usd({"cost_usd": 0}) == 0
    free = UsageInfo(input_tokens=1000, cost_usd=0)
    assert tracker.record_usage_info(free, model_id="claude-sonnet-4-6") == "provider"
    assert free.cost_usd == 0
    unknown = UsageInfo(input_tokens=100)
    tracker.record_usage_info(unknown, model_id="unknown-model")
    assert unknown.cost_usd is None
    assert tracker.get_summary()["unpriced_requests"] == 1


def test_side_calls_are_priced_by_their_own_models_before_aggregation(tracker):
    turn = LLMTurnContext(cost_session_id="cost-probe")
    context = LLMSideCallContext(SideQueryOptions(operation="compact"), {}, turn)
    LLMAdapter.record_non_stream_usage({"input_tokens": 1000, "output_tokens": 100},
        provider="anthropic", model_id="claude-haiku-4-5", input_includes_cache_read=False,
        input_includes_cache_write=False, context=context)
    main = UsageInfo(input_tokens=1000, output_tokens=100)
    tracker.record_usage_info(main, model_id="claude-sonnet-4-6", session_id="cost-probe")
    add_usage(turn.usage, main)
    assert turn.usage.cost_usd == pytest.approx(.0015 + .0045)
    assert tracker.get_summary("cost-probe")["priced_requests"] == 2
    assert context.record["price_source"] == "model_catalog"
    assert context.record["raw_usage"]["input_tokens"] == 1000


def test_declared_model_prices_and_explicit_free_prices(tracker):
    usage = UsageInfo(input_tokens=100, output_tokens=10)
    rates = {"input": 2, "output": 4, "cacheRead": .1, "cacheWrite": 3}
    assert tracker.record_usage_info(usage, model_id="custom", model_cost=rates) == "model_definition"
    assert usage.cost_usd == pytest.approx(.00024)
    free = UsageInfo(input_tokens=100)
    tracker.record_usage_info(free, model_id="custom", model_cost=dict.fromkeys(rates, 0))
    assert free.cost_usd == 0


def completed(text="answer"):
    return {"type": "response.completed", "response": {"id": "resp_probe", "status": "completed",
        "usage": {"input_tokens": 100, "output_tokens": 20},
        "output": [{"id": "msg_1", "type": "message", "role": "assistant", "phase": "final_answer",
                    "content": [{"type": "output_text", "text": text}]}]}}


@pytest.mark.asyncio
@pytest.mark.parametrize("report_usage", [True, False])
async def test_responses_stops_at_semantic_completion_and_preserves_final_phase(report_usage):
    async with httpx.AsyncClient() as client:
        adapter = OpenAIAdapter(LLMSettings(api_key="fixture", model="fixture", base_url="https://fixture.invalid/v1", wire_api="responses"), http_client=client)
        polled_after_done = False
        async def frames():
            nonlocal polled_after_done
            terminal = completed("Hello world")
            if not report_usage:
                terminal["response"].pop("usage")
            for frame in [
                {"type": "response.output_item.added", "output_index": 0, "item": {"id": "msg_1", "type": "message", "phase": "final_answer"}},
                {"type": "response.output_text.delta", "item_id": "msg_1", "output_index": 0, "content_index": 0, "delta": "Hello "},
                {"type": "response.output_text.done", "item_id": "msg_1", "output_index": 0, "content_index": 0, "text": "Hello world"},
                terminal,
            ]:
                yield _json_to_namespace(frame)
            polled_after_done = True
            raise RuntimeError("transport failed after completed")
        async def create(*args, **kwargs):
            return frames()
        adapter._create_responses_request = create
        events = [item async for item in adapter.stream_chat([LLMMessage("user", "fixture")])]
    assert not polled_after_done
    assert events[-1].type is StreamEventType.DONE
    assert (events[-1].usage is not None) is report_usage
    recovered = next(item for item in events if item.type is StreamEventType.TEXT_CHUNK and item.content == "world")
    assert recovered.phase == "final_answer"


@pytest.mark.asyncio
async def test_rejected_chat_terminal_exposes_reported_usage():
    frames = [
        {"choices": [{"delta": {"content": "hello"}, "finish_reason": None}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"prompt_tokens": 1000, "completion_tokens": 100, "cost_usd": .05}},
    ]
    body = "".join("data: " + json.dumps(frame) + "\n\n" for frame in frames) + "data: [DONE]\n\n"
    async def handler(request):
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = OpenAIAdapter(LLMSettings(api_key="fixture", model="fixture", base_url="https://fixture.invalid/v1", wire_api="chat"), http_client=client)
        events = [item async for item in adapter.stream_chat([LLMMessage("user", "fixture")])]
    assert events[-1].type is StreamEventType.ERROR
    usage = next(item.usage for item in events if item.type is StreamEventType.USAGE)
    assert (usage.input_tokens, usage.output_tokens, usage.cost_usd) == (1000, 100, .05)


@pytest.mark.asyncio
@pytest.mark.parametrize("report_usage", [True, False])
async def test_anthropic_stops_at_message_stop_and_normalizes_incremental_usage(report_usage):
    frames = [
        {"type": "message_start", "message": {"id": "msg_1", "role": "assistant", "content": [], "usage": {"input_tokens": 50, "output_tokens": 0, "cache_read_input_tokens": 900, "cache_creation_input_tokens": 300}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "answer"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 10}},
        {"type": "message_stop"},
    ]
    if not report_usage:
        frames[0]["message"].pop("usage")
        frames[-2].pop("usage")
    class Body(httpx.AsyncByteStream):
        polled_after_done = False
        async def __aiter__(self):
            yield "".join("data: " + json.dumps(frame) + "\n\n" for frame in frames).encode()
            self.polled_after_done = True
            raise RuntimeError("late transport error")
    body = Body()
    async def handler(request):
        return httpx.Response(200, stream=body, headers={"content-type": "text/event-stream"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = AnthropicAdapter(api_key="fixture", model="fixture", base_url="https://fixture.invalid")
        adapter._http_client = client
        events = [item async for item in adapter.stream_chat([LLMMessage("user", "fixture")])]
    assert not body.polled_after_done
    assert events[-1].type is StreamEventType.DONE
    if report_usage:
        assert events[-1].usage.input_tokens == 1250
        assert events[-1].raw["usage"]["input_tokens"] == 50
        assert events[-1].usage.billable_tokens == 360
    else:
        assert events[-1].usage is None


def test_native_replay_preserves_nulls_array_positions_and_deep_values():
    deep = {"value": None}
    for _ in range(16):
        deep = {"child": deep}
    native = [{"type": "tool_use", "id": "tool1", "name": "fixture", "input": {"nullable": None, "values": ["a", None, "b"], "deep": deep}}]
    assert _anthropic_replay_content([{"type": "anthropic_message", "content": native}]) == native
    with pytest.raises(ValueError, match="arguments must be an object"):
        _responses_tool_calls_from_provider_items([{"type": "function_call", "call_id": "call1", "name": "fixture", "arguments": "[1,2]"}])


@pytest.mark.asyncio
async def test_reasoning_whitespace_survives_public_projection():
    parts = []
    for text in ["Hello", " ", "world", "\n", "Next line"]:
        event = StreamEvent(StreamEventType.THINKING_CHUNK, content=text, raw={"provider_reasoning_type": "reasoning_summary_text"})
        async for item in project_non_text_provider_event(event, stream_state=StreamAttemptState(), stream_text=StreamTextState(),
                live_text_streaming=True, tool_tracker=SimpleNamespace(), tool_registry=None, tool_context=None,
                process_event_factory=lambda *args, **kwargs: None):
            if getattr(item, "type", None) == "thinking_delta":
                parts.append(item.data["content"])
    assert "".join(parts) == "Hello world\nNext line"


@pytest.mark.asyncio
@pytest.mark.parametrize("use_small_model", [False, True])
@pytest.mark.parametrize("report_usage", [False, True])
async def test_chat_side_policy_disables_declared_reasoning_and_prices_once(tracker, use_small_model, report_usage):
    captured = {}
    async def handler(request):
        captured.update(json.loads(request.content))
        frame = {"choices": [{"delta": {"content": "summary"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 3, "completion_tokens": 2}}
        if not report_usage:
            frame.pop("usage")
        return httpx.Response(200, content="data: " + json.dumps(frame) + "\n\ndata: [DONE]\n\n")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = OpenAIAdapter(LLMSettings(api_key="fixture", model="fixture", small_fast_model="fixture-small", base_url="https://fixture.invalid/v1", wire_api="chat",
                reasoning_effort="high", reasoning_effort_levels=("none", "low", "high")), http_client=client)
        adapter._request_model_costs = {
            "fixture": {"input": 10, "output": 20, "cacheRead": 0, "cacheWrite": 0},
            "fixture-small": {"input": 1, "output": 2, "cacheRead": 0, "cacheWrite": 0},
        }
        turn = LLMTurnContext()
        assert await adapter.side_query([LLMMessage("user", "Summarize")], options=SideQueryOptions(operation="compact", disable_reasoning=True, use_small_fast_model=use_small_model), turn_context=turn) == "summary"
    if use_small_model:
        assert "reasoning_effort" not in captured
    else:
        assert captured["reasoning_effort"] == "none"
    assert captured["model"] == ("fixture-small" if use_small_model else "fixture")
    assert turn.usage.input_tokens == (3 if report_usage else 0)
    if report_usage:
        assert turn.usage.cost_usd == pytest.approx(.000007 if use_small_model else .00007)
    else:
        assert turn.usage.cost_usd is None
    assert tracker.get_summary()["priced_requests"] == int(report_usage)
    assert tracker.get_summary()["unpriced_requests"] == int(not report_usage)


@pytest.mark.asyncio
async def test_side_failure_keeps_usage_before_error(tracker):
    body = 'data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":2,"cost_usd":0.03}}\n\ndata: {"error":{"message":"invalid api key","type":"authentication_error"}}\n\n'
    async def handler(request):
        return httpx.Response(200, content=body)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = OpenAIAdapter(LLMSettings(api_key="fixture", model="fixture", base_url="https://fixture.invalid/v1", wire_api="chat"), http_client=client)
        turn = LLMTurnContext()
        with pytest.raises(RuntimeError):
            await adapter.side_query([LLMMessage("user", "fixture")], options=SideQueryOptions(operation="compact"), turn_context=turn)
    assert turn.usage.cost_usd == pytest.approx(.03)
    assert tracker.get_summary()["priced_requests"] == 1


def provider_run(monkeypatch, batches):
    import backend.agent.provider_stream_runtime as module
    calls = iter(batches)
    def stream(*args, **kwargs):
        batch = next(calls)
        async def generate():
            for event in batch:
                if isinstance(event, BaseException):
                    raise event
                yield event
        return generate()
    async def wait(**kwargs):
        try:
            yield ProviderWaitResult("event", await anext(kwargs["stream_iter"]))
        except StopAsyncIteration:
            yield ProviderWaitResult("finish")
    async def retry(event, **kwargs):
        yield ProviderErrorEventResult("retry", kwargs["stream_attempt"] + 1, None, False)
    async def exception(exc, **kwargs):
        if not isinstance(exc, asyncio.CancelledError):
            raise exc
        yield ProviderStreamExceptionResult(cancelled=True)
    monkeypatch.setattr(module, "safe_stream_chat_with_request_metadata", stream)
    monkeypatch.setattr(module, "wait_for_next_provider_event", wait)
    monkeypatch.setattr(module, "handle_provider_error_event", retry)
    monkeypatch.setattr(module, "handle_provider_stream_exception", exception)
    class Kernel:
        run_record = SimpleNamespace(run_id="r1")
        metadata = {}
        async def start_provider_attempt(self, **kwargs):
            return ProviderAttempt(kwargs["iteration_id"], kwargs["retry_index"], "span", kwargs["started_at"])
        async def observe_provider_first_event(self, *args, **kwargs):
            pass
        async def close_provider_attempt(self, attempt, **kwargs):
            attempt.closed = True
    class Completion:
        async def settle(self, event, **kwargs):
            stream_state = kwargs["stream_state"]
            stream_state.accept_provider_event(event)
            return ProviderCompletionResult(stream_state.usage, "stop", "", ())
    committed = []
    turn = LLMTurnContext()
    async def unused(*args, **kwargs):
        if False:
            yield None
    generator = stream_provider_response(
        llm=SimpleNamespace(), messages=[], tool_schemas=[], llm_request_metadata={}, prompt_cache_safe_params={},
        provider_completion=Completion(), state=SimpleNamespace(stopped_reason=None, iterations=1), context_builder=SimpleNamespace(),
        turn_kernel=Kernel(), budget_runtime=SimpleNamespace(ensure_started=lambda: None, cost_session_id="probe",
            record_provider_usage_total=lambda value: committed.append(value.total_tokens)), turn_usage=turn.usage,
        settings=SimpleNamespace(stream_max_attempts=1, live_text_streaming=True), tool_registry=None, permission_checker=None,
        effective_permission_context=None, tool_context=SimpleNamespace(cancel_event=asyncio.Event()), turn_start_tool_call_count=0,
        turn_started_at=0, iteration_limit=10, tool_batch_count=0, iteration_id_value="i1", stream_retry_policy=None, error_controller=None,
        chain=SimpleNamespace(record_usage=lambda **kwargs: None), stream_text=StreamTextState(), degrade_and_finish=unused, recover_withheld_error=None)
    return generator, turn, committed


@pytest.mark.asyncio
async def test_missing_main_usage_is_an_unpriced_request(monkeypatch, tracker):
    generator, turn, committed = provider_run(monkeypatch, [[StreamEvent(StreamEventType.DONE)]])
    _ = [event async for event in generator]
    assert turn.usage.cost_usd is None
    assert tracker.get_summary()["unpriced_requests"] == 1


@pytest.mark.asyncio
async def test_request_usage_is_settled_once_across_usage_done_error_and_retry(monkeypatch, tracker):
    first = UsageInfo(input_tokens=100, output_tokens=10, cost_usd=.1)
    second = UsageInfo(input_tokens=200, output_tokens=20, cost_usd=.2)
    generator, turn, committed = provider_run(monkeypatch, [
        [StreamEvent(StreamEventType.USAGE, usage=first), StreamEvent(StreamEventType.ERROR, content="network")],
        [StreamEvent(StreamEventType.USAGE, usage=second), StreamEvent(StreamEventType.DONE, usage=second)],
    ])
    events = [event async for event in generator]
    assert isinstance(events[-1], ProviderStreamResult)
    assert turn.usage.input_tokens == 300
    assert turn.usage.cost_usd == pytest.approx(.3)
    assert committed == [110, 330]
    assert tracker.get_summary()["priced_requests"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("consumer_close", [False, True])
async def test_cancellation_and_consumer_close_keep_reported_usage(monkeypatch, tracker, consumer_close):
    usage = UsageInfo(input_tokens=100, cost_usd=.1)
    generator, turn, committed = provider_run(monkeypatch, [[StreamEvent(StreamEventType.USAGE, usage=usage, raw={"provider": "fixture"}), asyncio.CancelledError()]])
    if consumer_close:
        await anext(generator)
        await generator.aclose()
    else:
        with pytest.raises(asyncio.CancelledError):
            _ = [event async for event in generator]
    assert turn.usage.input_tokens == 100
    assert turn.usage.cost_usd == pytest.approx(.1)
    assert committed == [100]
    assert tracker.get_summary()["priced_requests"] == 1


@pytest.mark.asyncio
async def test_timeout_recovery_does_not_charge_the_settled_request_again():
    turn = UsageInfo(input_tokens=100, cost_usd=.1)
    async def close(*args, **kwargs):
        pass
    async def degrade(**kwargs):
        assert kwargs["usage"].input_tokens == 100
        if False:
            yield None
    _ = [event async for event in recover_stream_timeout(turn_kernel=SimpleNamespace(close_provider_attempt=close), provider_attempt=None,
        state=SimpleNamespace(), context_builder=SimpleNamespace(), turn_usage=turn, usage=UsageInfo(input_tokens=100, cost_usd=.1),
        stream_state=SimpleNamespace(saw_partial_tool_call=False), stream_text=StreamTextState(), pending_tool_calls=[], degrade_and_finish=degrade)]
    assert turn.input_tokens == 100


@pytest.mark.asyncio
async def test_retired_runtime_saves_already_rotated_oauth_credentials(monkeypatch):
    monkeypatch.setattr(ModelRuntime, "_load_base_providers", lambda self: {})
    runtime = ModelRuntime(models_store=SimpleNamespace(), provider_configs={})
    class Store:
        credential = {"type": "oauth", "access": "old", "refresh": "old", "expires": 0}
        def get(self, provider):
            return dict(self.credential)
        async def modify(self, provider, callback):
            changed = await callback(dict(self.credential))
            if changed is not None:
                self.credential = dict(changed)
            return dict(self.credential)
    store = Store()
    runtime._auth_storage = store
    started, response = asyncio.Event(), asyncio.Event()
    async def refresh(credential):
        started.set()
        await response.wait()
        return {"access": "new", "refresh": "new", "expires": 9999999999999}
    runtime._extension_providers["fixture"] = {"oauth": {"refresh": refresh}}
    task = asyncio.create_task(runtime.refresh_oauth_credentials("fixture"))
    await started.wait()
    runtime.retire()
    response.set()
    with pytest.raises(RuntimeError, match="retired"):
        await task
    assert store.credential["refresh"] == "new"
