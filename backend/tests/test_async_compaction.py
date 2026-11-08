from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
import json
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio

from backend.agent.async_compaction import AsyncCompaction
from backend.agent.context import ContextBuilder, clone_context_builder
from backend.agent.execution_journal import ExecutionJournal
from backend.agent.model_execution import ModelExecutionSnapshot
from backend.agent.rollout_budget import RolloutBudget
from backend.agent.run_context import RunContext
from backend.agent.state import AgentState
from backend.config import AppConfig, LLMSettings, TokenBudget
from backend.config_layers import ConfigLayer, ConfigLayerSource, ConfigLayerStack
from backend.feature_flags import FeatureFlags, load_feature_flags
from backend.llm.base import LLMMessage, LLMTurnContext
from backend.llm.native_compaction import native_compaction_windows
from backend.llm.openai_adapter import OpenAIAdapter
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.services.context_budget import manage_context_budget
from backend.ws.run_manager import SessionRunManager


class Endpoint:
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = asyncio.Event()
        self.requests = []
        self.fail = False

    async def __call__(self, request):
        self.requests.append(json.loads(request.content))
        endpoint = self

        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                endpoint.started.set()
                await endpoint.release.wait()
                item = {"type": "compaction", "encrypted_content": "prepared-native"}
                if not endpoint.fail:
                    yield ("data: " + json.dumps({"type": "response.output_item.done", "item": item}) + "\n\n").encode()
                yield ("data: " + json.dumps({"type": "response.completed", "response": {"id": "background-result",
                    "usage": {"input_tokens": 700, "output_tokens": 30}, "output": [item]}}) + "\n\n").encode()

            async def aclose(self):
                endpoint.closed.set()

        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Stream())


@pytest_asyncio.fixture
async def fixture(tmp_path):
    endpoint = Endpoint()
    client = httpx.AsyncClient(transport=httpx.MockTransport(endpoint))
    settings = LLMSettings(provider="custom", model="background-fixture", api_key="fixture",
        base_url="https://fixture.invalid/v1", wire_api="responses", context_window=100_000, max_tokens=1024)
    adapter = OpenAIAdapter(settings, http_client=client)
    adapter.configure_remote_compaction("v2")
    config = AppConfig(llm=settings, token_budget=TokenBudget(total=100_000, response_reserve=4096),
        feature_flags=FeatureFlags({"async_compaction": True}),
        config_layer_stack=ConfigLayerStack((ConfigLayer(ConfigLayerSource(kind="user"),
            {"features": {"async_compaction": {"enabled": True, "model_async_compact_threshold_percent": 1}}}),)))
    execution = ModelExecutionSnapshot(config=config, llm=adapter, provider="custom", model=settings.model)
    builder = ContextBuilder(llm=adapter, token_budget=config.token_budget)
    builder._history_store.append(LLMMessage("user", "Retained task", is_user_input=True))
    builder.append_assistant("evidence " * 1500)
    builder._async_compaction = AsyncCompaction()
    builder._async_compaction_execution = execution
    run = RunContext(model_execution=execution, active_model_execution=execution,
        execution_journal=ExecutionJournal("background", base_dir=tmp_path / "journals"),
        llm_turn_context=LLMTurnContext(cost_session_id="owned-session"))
    cancel = asyncio.Event()
    context = ToolExecutionContext(permission=PermissionContext(mode="bypass"), cancel_event=cancel,
        model_execution=execution, llm=adapter, run_context=run)
    builder._tool_execution_context = context
    state = AgentState(user_message="Continue")
    budget = RolloutBudget()
    result = SimpleNamespace(endpoint=endpoint, adapter=adapter, execution=execution, builder=builder,
        owner=builder._async_compaction, run=run, context=context, cancel=cancel, state=state, budget=budget,
        metadata={"session_id": "owned-session", "thread_id": "owned-thread", "turn_id": "owned-turn"})
    try:
        yield result
    finally:
        endpoint.release.set()
        await result.owner.aclose()
        await adapter.aclose()
        await client.aclose()


def launch(f, *, phase="post_turn"):
    f.owner.maybe_start(f.builder, f.state, execution=f.execution, tools=[], metadata=f.metadata,
        run_context=f.run, rollout_budget=f.budget, phase=phase)


async def finish(f):
    f.endpoint.release.set()
    await asyncio.gather(*f.owner.pending_tasks())


@pytest.mark.asyncio
async def test_actual_pending_request_outlives_stop_and_preserves_the_live_suffix(fixture):
    f = fixture
    f.cancel.set()
    launch(f)
    assert f.owner.current is None
    f.cancel.clear()
    launch(f)
    await asyncio.wait_for(f.endpoint.started.wait(), 5)
    original = deepcopy(f.builder.export_snapshot()["history"])
    f.cancel.set()
    launch(f)
    assert len(f.endpoint.requests) == 1 and not f.endpoint.closed.is_set()
    f.builder._history_store.append(LLMMessage("user", "Accepted later", is_user_input=True))
    # Admission annotates/recounts the newly appended input. This changes the
    # general history revision without replacing the captured prefix.
    f.builder._history[-1].accepted_input = {"message_id": "later-owned-input"}
    f.builder._history_store.refresh_message_estimate(f.builder._history[-1])
    suffix = deepcopy(f.builder.export_snapshot()["history"][-1])
    await finish(f)
    assert f.builder.export_snapshot()["history"][:len(original)] == original
    f.cancel.clear()
    events = [event async for event in manage_context_budget(f.builder, f.state, f.execution.config.token_budget, [])]
    assert [event.type for event in events].count("context_compacted") == 1
    assert f.builder.export_snapshot()["history"][-1] == suffix
    assert native_compaction_windows(f.builder._history[0])[0]["output"][-1]["encrypted_content"] == "prepared-native"
    assert len(f.endpoint.requests) == 1 and f.budget.tokens_used() == 730
    records = f.run.execution_journal.read_events()
    assert any(event.payload.get("lifecycle") == "background_compaction_finished" for event in records)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["rewrite", "api_key", "model", "comp_hash", "auth_owner"])
async def test_stale_results_keep_usage_and_do_not_replace_changed_context(fixture, change):
    f = fixture
    launch(f)
    await asyncio.wait_for(f.endpoint.started.wait(), 5)
    if change == "rewrite":
        f.builder._history = [LLMMessage("user", "Replaced by another operation", is_user_input=True)]
    elif change == "auth_owner":
        f.adapter._provider_request_auth = object()
    else:
        f.adapter._settings = replace(f.adapter._settings, **{change: "changed"})
    before = deepcopy(f.builder.export_snapshot()["history"])
    await finish(f)
    assert not f.owner.ready(f.builder, f.execution)
    assert await f.owner.take(f.builder, f.execution, wait=True) is None
    assert f.builder.export_snapshot()["history"] == before
    assert f.budget.tokens_used() == 730


@pytest.mark.asyncio
async def test_failed_candidate_is_observed_once_then_foreground_can_recover(fixture):
    f = fixture
    f.endpoint.fail = True
    launch(f)
    await finish(f)
    assert f.owner.current.error and not f.owner.ready(f.builder, f.execution)
    assert f.owner.current is None
    launch(f)
    assert len(f.endpoint.requests) == 1
    f.endpoint.fail = False
    await f.builder.compact(tool_schemas=[])
    assert len(f.endpoint.requests) == 2
    assert native_compaction_windows(f.builder._history[0])
    assert f.budget.tokens_used() == 730


@pytest.mark.asyncio
async def test_foreground_waiter_cancellation_does_not_cancel_background(fixture):
    f = fixture
    launch(f)
    await asyncio.wait_for(f.endpoint.started.wait(), 5)
    waiter = asyncio.create_task(f.builder.compact(tool_schemas=[]))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert f.owner.pending_tasks() and not f.endpoint.closed.is_set()
    await finish(f)
    await f.builder.compact(tool_schemas=[])
    assert len(f.endpoint.requests) == 1


@pytest.mark.asyncio
async def test_shutdown_closes_request_and_fences_new_launches(fixture):
    f = fixture
    launch(f)
    await asyncio.wait_for(f.endpoint.started.wait(), 5)
    await f.owner.aclose()
    assert f.endpoint.closed.is_set() and not f.owner.pending_tasks()
    launch(f)
    assert len(f.endpoint.requests) == 1


@pytest.mark.asyncio
async def test_manual_clone_can_adopt_exact_retained_prefix(fixture):
    f = fixture
    launch(f)
    await finish(f)
    manual = clone_context_builder(f.builder)
    assert manual._async_compaction is None
    manual._async_compaction = f.owner
    manual._async_compaction_execution = f.execution
    await manual.compact(tool_schemas=[])
    assert native_compaction_windows(manual._history[0])
    assert not native_compaction_windows(f.builder._history[0])
    assert len(f.endpoint.requests) == 1


@pytest.mark.asyncio
async def test_pre_turn_requires_a_real_route_and_default_feature_does_not_launch(fixture):
    f = fixture
    launch(f, phase="pre_turn")
    assert f.owner.current is None
    f.adapter._remember_responses_turn_state(f.metadata, {"x-codex-turn-state": "real-route"})
    f.execution = replace(f.execution, config=replace(f.execution.config, feature_flags=FeatureFlags({})))
    launch(f, phase="pre_turn")
    assert f.owner.current is None
    f.execution = replace(f.execution, config=replace(f.execution.config, feature_flags=FeatureFlags({"async_compaction": True})))
    launch(f, phase="pre_turn")
    await finish(f)
    assert len(f.endpoint.requests) == 1


@pytest.mark.asyncio
async def test_model_lease_and_lifecycle_generation_wait_for_background_without_marking_a_foreground_run(fixture):
    f = fixture
    from backend.ws.agent_runner import _lease_session_llm_for_task, _clear_session_llm_cache

    session = SimpleNamespace(session_id="lease", _llm_adapter_cache={"active": f.adapter}, _llm_adapter_leases={},
        _retired_llm_adapters={}, _llm_close_tasks=set())
    manager = SessionRunManager(session)
    f.run.retain_model = lambda adapter, task: _lease_session_llm_for_task(session, adapter, task)
    f.run.retain_background_model = lambda task: manager.retain_background_model_task("thread", task)
    launch(f)
    await asyncio.wait_for(f.endpoint.started.wait(), 5)
    _clear_session_llm_cache(session)
    assert not f.adapter._closed and not manager.run_tasks
    fence = manager.lifecycle_release_fence("thread")
    assert fence is not None and not fence.done()
    await manager.close_background_models("thread")
    await fence
    await asyncio.gather(*session._llm_close_tasks)
    assert f.adapter._closed and f.endpoint.closed.is_set()


@pytest.mark.parametrize("value", [0, 101, True, "65", 2.5])
def test_threshold_validates_source_configuration(value):
    with pytest.raises(ValueError, match="between 1 and 100"):
        load_feature_flags({"features": {"async_compaction": {"enabled": True, "model_async_compact_threshold_percent": value}}}, managed_requirements={})


@pytest.mark.asyncio
async def test_exhausted_foreground_reuses_matching_pending_request(fixture):
    f = fixture
    launch(f)
    await asyncio.wait_for(f.endpoint.started.wait(), 5)
    f.builder._last_actual_prompt_tokens = 100_001
    consume = asyncio.create_task(_budget_events(f))
    await asyncio.sleep(0.02)
    assert not consume.done() and len(f.endpoint.requests) == 1
    f.endpoint.release.set()
    emitted = await consume
    assert any(event.type == "context_compacted" for event in emitted)
    assert len(f.endpoint.requests) == 1


async def _budget_events(f):
    return [event async for event in manage_context_budget(f.builder, f.state, f.execution.config.token_budget, [])]


@pytest.mark.asyncio
async def test_late_background_usage_reaches_the_existing_goal_after_foreground_finish(fixture, tmp_path):
    from backend.conversations.repository import ConversationRepository
    from backend.services.goal_runtime import GoalTurnAccounting, new_goal

    f = fixture
    repo = ConversationRepository(tmp_path / "goal-conversations")
    record = repo.create_conversation()
    repo.mutate_goal(record.id, lambda _: new_goal("actual local goal"))
    f.run.goal_accounting = GoalTurnAccounting(repo, record.id)
    launch(f)
    await asyncio.wait_for(f.endpoint.started.wait(), 5)
    f.run.goal_accounting.finish(status="completed", content="foreground finished")
    assert repo.get_conversation_summary(record.id).goal["tokens_used"] == 0
    await finish(f)
    assert repo.get_conversation_summary(record.id).goal["tokens_used"] == 730
    f.run.goal_accounting.account()
    assert repo.get_conversation_summary(record.id).goal["tokens_used"] == 730


@pytest.mark.asyncio
async def test_history_replacement_while_waiting_rejects_prepared_prefix(fixture):
    f = fixture
    launch(f)
    await asyncio.wait_for(f.endpoint.started.wait(), 5)
    waiting = asyncio.create_task(f.builder.compact(tool_schemas=[]))
    await asyncio.sleep(0)
    f.builder._history = [LLMMessage("user", "New authoritative history", is_user_input=True)]
    f.endpoint.release.set()
    await waiting
    assert len(f.endpoint.requests) == 2
    assert "New authoritative history" in json.dumps(f.endpoint.requests[-1])
    assert "Retained task" not in json.dumps(f.endpoint.requests[-1])
    assert "New authoritative history" in json.dumps(native_compaction_windows(f.builder._history[0]))


@pytest.mark.asyncio
async def test_ready_candidate_still_over_budget_uses_one_foreground_compaction(fixture):
    f = fixture
    launch(f)
    await finish(f)
    f.builder._history_store.append(LLMMessage("user", "late suffix " * 12_000, is_user_input=True))
    f.builder.bind_budget(TokenBudget(total=20_000, response_reserve=4096))
    await f.builder.compact(tool_schemas=[])
    assert len(f.endpoint.requests) == 2
    assert f.builder._compaction_count == 2
    assert "late suffix" in json.dumps(f.endpoint.requests[-1])


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_kind", ["desktop", "sdk"])
async def test_thread_owner_shutdown_and_sdk_session_shutdown_drain_actual_request(fixture, tmp_path, owner_kind):
    from backend.ws.execution_context import ConversationThreadRuntime
    from backend.agent.query_engine import AgentSession
    from backend.artifact.store import ArtifactStore
    from backend.permissions.checker import PermissionChecker
    from backend.tools.registry import ToolRegistry

    f = fixture
    if owner_kind == "desktop":
        thread = ConversationThreadRuntime("fixture-store", "fixture-thread", async_compaction=f.owner)
        close = thread.close_environments
    else:
        session = AgentSession(llm=f.adapter, tool_registry=ToolRegistry(),
            artifact_store=ArtifactStore(storage_dir=tmp_path / "artifacts"), owns_artifact_store=True,
            permission_checker=PermissionChecker(f.execution.config.permissions),
            agent_settings=f.execution.config.agent, token_budget=f.execution.config.token_budget,
            context_builder=f.builder, async_compaction=f.owner)
        close = session.aclose
    launch(f)
    await asyncio.wait_for(f.endpoint.started.wait(), 5)
    await close()
    assert f.owner.closing and not f.owner.pending_tasks() and f.endpoint.closed.is_set()


@pytest.mark.asyncio
async def test_scheduled_request_keeps_captured_model_before_its_task_first_runs(fixture):
    f = fixture
    launch(f)
    # No yield occurred between capture and the new host selection.
    f.adapter._settings = replace(f.adapter._settings, model="later-model", base_url="https://later.invalid/v1")
    await finish(f)
    assert f.endpoint.requests[0]["model"] == "background-fixture"
    assert not f.owner.ready(f.builder, f.execution)
    assert f.budget.tokens_used() == 730
