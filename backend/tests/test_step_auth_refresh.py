from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace

import httpx
import pytest

from backend.agent.loop_session import AgentLoopSessionContext
from backend.agent.model_execution import ModelExecutionSnapshot
from backend.agent.query_engine import QueryEngine, QuerySubmission
from backend.agent.state import AgentState
from backend.llm.base import StreamEvent, StreamEventType, ToolCallEvent
from backend.llm.openai_adapter import OpenAIAdapter
from backend.services.llm_adapter_factory import create_session_llm
from backend.tests.test_model_execution_ownership import setup
from backend.tests.test_model_runtime import _modern_api_key_runtime, _oauth_runtime
from backend.ws.agent_runner import _lease_session_llm_for_task, _refresh_session_model_auth


def response(*, tool=False):
    delta = ({"tool_calls": [{"index": 0, "id": "inspect-1", "type": "function",
        "function": {"name": "inspect_model", "arguments": "{}"}}]} if tool else
        {"content": "Authentication refresh verified."})
    chunk = {"choices": [{"delta": delta, "finish_reason": "tool_calls" if tool else "stop"}]}
    return httpx.Response(200, content="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n",
        headers={"content-type": "text/event-stream"})


async def fixture_for(tmp_path, monkeypatch, runtime, provider, client, *, retries=2):
    async def unused(*args):
        raise AssertionError("Unexpected fixture model request")
    fixture = setup(tmp_path, monkeypatch, unused)
    fixture.adapters = []
    def build(settings):
        adapter = OpenAIAdapter(settings, http_client=client)
        fixture.adapters.append(adapter)
        return adapter
    monkeypatch.setattr("backend.services.llm_adapter_factory.OpenAIAdapter", build)
    await runtime.refresh_provider_auth(provider)
    config = replace(fixture.config,
        llm=replace(fixture.config.llm, provider=provider, model="model-1", reasoning_effort="off"),
        agent=replace(fixture.config.agent, stream_max_attempts=retries))
    adapter = create_session_llm(config, model_override="model-1", model_runtime=runtime)
    fixture.owner.model_execution = ModelExecutionSnapshot(config=config, llm=adapter, provider=provider,
        model="model-1", model_runtime=runtime, model_info=runtime.get_model(provider, "model-1"))
    fixture.owner.retain_model = lambda adapter, task: _lease_session_llm_for_task(fixture.host, adapter, task)
    fixture.owner.refresh_model_auth = lambda snapshot, force, task: _refresh_session_model_auth(fixture.host, snapshot, force, task)
    fixture.session.llm = adapter
    fixture.session.agent_settings = config.agent
    fixture.builder.bind_llm(adapter)
    return fixture


async def run_query(fixture, tmp_path, *, cancel_event=None):
    state = AgentState(user_message="Inspect the model and finish", workspace_root=tmp_path, conversation_id="model-conv")
    async def consume():
        return [event async for event in QueryEngine().submit(QuerySubmission(session=fixture.session, state=state,
            user_message=state.user_message, runtime=AgentLoopSessionContext(workspace_root=tmp_path,
                session_id="auth-session", run_context=fixture.owner, cancel_event=cancel_event)))]
    task = asyncio.create_task(consume())
    try:
        events = await task
    finally:
        await fixture.session.aclose()
        fixture.runtime.close(release_lease=True)
        await asyncio.sleep(0)
        await asyncio.gather(*getattr(fixture.host, "_llm_close_tasks", ()))
    return state, events


@pytest.mark.asyncio
async def test_long_query_refreshes_auth_between_tool_steps_and_closes_owned_adapter(tmp_path, monkeypatch):
    version = 0
    async def resolve(_input):
        return {"auth": {"api_key": f"fixture-{version}", "headers": {"X-Account": str(version)},
            "base_url": f"https://route-{version}.invalid/v1"}}
    runtime, _ = _modern_api_key_runtime({"resolve": resolve})
    seen = []
    async def endpoint(request):
        nonlocal version
        seen.append((request.headers["Authorization"], request.headers["X-Account"], request.url.host))
        if len(seen) == 1:
            version = 1
            return response(tool=True)
        assert not fixture.adapters[0]._closed
        assert not fixture.adapters[1]._closed
        return response()
    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as client:
        fixture = await fixture_for(tmp_path, monkeypatch, runtime, "modern-auth", client)
        old_snapshot = fixture.owner.model_execution
        state, _ = await run_query(fixture, tmp_path)
        assert state.terminal_status == "completed"
        assert seen == [("Bearer fixture-0", "0", "route-0.invalid"), ("Bearer fixture-1", "1", "route-1.invalid")]
        assert len(fixture.inspector.observations) == 1
        assert fixture.owner.model_execution.llm is fixture.adapters[1]
        assert old_snapshot.llm.provider_adapter_spec.api_key == "fixture-0"
        assert fixture.adapters[1]._closed
        assert not fixture.adapters[0]._closed  # supplied initial adapter remains borrowed
        assert not client.is_closed
        await fixture.adapters[0].aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("status,retries,rotate,expected_requests", [(401, 2, True, 2), (401, 0, True, 1), (403, 2, True, 1), (401, 2, False, 1)])
async def test_http_auth_recovery_only_replays_401_with_new_credentials(tmp_path, monkeypatch, status, retries, rotate, expected_requests):
    version = 0
    async def resolve(_input):
        return {"auth": {"api_key": f"fixture-{version}"}}
    runtime, _ = _modern_api_key_runtime({"resolve": resolve})
    seen = []
    async def endpoint(request):
        nonlocal version
        seen.append((request.headers["Authorization"], json.loads(request.content)))
        if len(seen) == 1:
            version = int(rotate)
            return httpx.Response(status, json={"error": {"message": "Rejected fixture credential"}})
        return response()
    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as client:
        fixture = await fixture_for(tmp_path, monkeypatch, runtime, "modern-auth", client, retries=retries)
        state, _ = await run_query(fixture, tmp_path)
        assert len(seen) == expected_requests
        assert (state.terminal_status == "completed") == (expected_requests == 2)
        if expected_requests == 2:
            assert [item[0] for item in seen] == ["Bearer fixture-0", "Bearer fixture-1"]
            assert seen[0][1] == seen[1][1]
            assert state.total_retries == 1
        await fixture.adapters[0].aclose()


@pytest.mark.asyncio
async def test_repeated_401_stops_after_one_auth_replay(tmp_path, monkeypatch):
    version = 0
    async def resolve(_input):
        return {"auth": {"api_key": f"fixture-{version}"}}
    runtime, _ = _modern_api_key_runtime({"resolve": resolve})
    seen = []
    async def endpoint(request):
        nonlocal version
        seen.append(request.headers["Authorization"])
        version += 1
        return httpx.Response(401, json={"error": {"message": "Rejected fixture credential"}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as client:
        fixture = await fixture_for(tmp_path, monkeypatch, runtime, "modern-auth", client, retries=5)
        state, _ = await run_query(fixture, tmp_path)
        assert state.terminal_status == "failed"
        assert state.total_retries == 1
        assert seen == ["Bearer fixture-0", "Bearer fixture-1"]
        await fixture.adapters[0].aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["text", "tool", "image", "activity"])
async def test_401_after_provider_output_never_replays(tmp_path, monkeypatch, prefix):
    async def unused(*args):
        raise AssertionError("Unexpected fixture call")
    fixture = setup(tmp_path, monkeypatch, unused)
    fixture.session.agent_settings = replace(fixture.config.agent, stream_max_attempts=3)
    refreshes, requests = [], []
    async def refresh(snapshot, force, task):
        refreshes.append(force)
        return snapshot
    fixture.owner.refresh_model_auth = refresh
    async def stream(messages, tools=None, metadata=None):
        requests.append(messages)
        if prefix == "text":
            yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="Partial answer")
        elif prefix == "tool":
            yield StreamEvent(type=StreamEventType.TOOL_CALL, tool_calls=[ToolCallEvent(id="once", name="inspect_model", arguments={})], tool_calls_committed=True)
        elif prefix == "image":
            yield StreamEvent(type=StreamEventType.IMAGE_CHUNK, image_data="aW1hZ2U=")
        else:
            yield StreamEvent(type=StreamEventType.PROVIDER_ACTIVITY)
        yield StreamEvent(type=StreamEventType.ERROR, content="Unauthorized", raw={"status_code": 401})
    fixture.model.stream_chat = stream
    state, _ = await run_query(fixture, tmp_path)
    assert state.terminal_status in {"failed", "partial"}
    assert len(requests) == 1
    assert refreshes == [False]
    assert len(fixture.inspector.observations) <= 1


@pytest.mark.asyncio
async def test_selection_during_step_auth_refresh_wins(tmp_path, monkeypatch):
    async def behavior(*args):
        return None
    fixture = setup(tmp_path, monkeypatch, behavior)
    original = fixture.owner.model_execution
    newer = replace(original, thinking_level="high", config=replace(original.config,
        llm=replace(original.config.llm, reasoning_effort="high")))
    seen = []
    async def refresh(snapshot, force, task):
        seen.append(snapshot)
        if snapshot is original:
            fixture.owner.model_execution = newer
            await asyncio.sleep(0)
            return replace(original, thinking_level="low")
        return snapshot
    fixture.owner.refresh_model_auth = refresh
    state, _ = await run_query(fixture, tmp_path)
    assert state.terminal_status == "completed"
    assert seen == [original, newer]
    assert fixture.owner.model_execution is newer
    assert fixture.owner.active_model_execution is newer


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["admission", "401"])
@pytest.mark.parametrize("stop", ["cancel", "deadline"])
async def test_auth_refresh_obeys_query_cancellation_and_deadline(tmp_path, monkeypatch, phase, stop):
    entered, cancelled, cancel_event = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def resolve(_input):
        return {"auth": {"api_key": "fixture-0"}}
    runtime, _ = _modern_api_key_runtime({"resolve": resolve})
    requests = []
    async def endpoint(request):
        requests.append(request)
        return httpx.Response(401, json={"error": {"message": "Rejected fixture credential"}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as client:
        fixture = await fixture_for(tmp_path, monkeypatch, runtime, "modern-auth", client)
        async def refresh(snapshot, force, task):
            if phase == "401" and not force:
                return snapshot
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        fixture.owner.refresh_model_auth = refresh
        if stop == "deadline":
            fixture.session.agent_settings = replace(fixture.session.agent_settings, max_turn_seconds=2)
        task = asyncio.create_task(run_query(fixture, tmp_path, cancel_event=cancel_event))
        await asyncio.wait_for(entered.wait(), 3)
        if stop == "cancel":
            cancel_event.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 4)
        else:
            state, _ = await asyncio.wait_for(task, 4)
            assert state.stopped_reason == "max_turn_seconds"
        assert cancelled.is_set()
        assert len(requests) == (1 if phase == "401" else 0)
        assert len(fixture.adapters) == 1
        await fixture.adapters[0].aclose()


@pytest.mark.asyncio
async def test_forced_oauth_refresh_coalesces_under_existing_credential_transaction():
    entered, release = asyncio.Event(), asyncio.Event()
    rotations = []
    async def refresh(credential):
        rotations.append(credential["access"])
        entered.set()
        await release.wait()
        return {**credential, "access": "fixture-new", "expires": time.time() * 1000 + 60000}
    async def to_auth(credential):
        return {"api_key": credential["access"]}
    runtime, storage = _oauth_runtime({"login": refresh, "refresh": refresh, "to_auth": to_auth})
    storage.set("modern-oauth", {"type": "oauth", "access": "fixture-old", "refresh": "fixture-refresh", "expires": time.time() * 1000 + 60000})
    await runtime.refresh_provider_auth("modern-oauth", publish_snapshot=False)
    first = asyncio.create_task(runtime.refresh_provider_auth("modern-oauth", publish_snapshot=False, force=True))
    await entered.wait()
    second = asyncio.create_task(runtime.refresh_provider_auth("modern-oauth", publish_snapshot=False, force=True))
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(first, second)
    assert rotations == ["fixture-old"]
    assert runtime.resolve_provider_auth("modern-oauth")["auth"]["api_key"] == "fixture-new"


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger", ["expiry", "401"])
async def test_query_rotates_oauth_at_expiry_or_after_rejection(tmp_path, monkeypatch, trigger):
    rotations, seen = [], []
    async def refresh(credential):
        rotations.append(credential["access"])
        return {**credential, "access": "fixture-new", "expires": time.time() * 1000 + 60000}
    async def to_auth(credential):
        return {"api_key": credential["access"]}
    runtime, storage = _oauth_runtime({"login": refresh, "refresh": refresh, "to_auth": to_auth})
    storage.set("modern-oauth", {"type": "oauth", "access": "fixture-old", "refresh": "fixture-refresh", "expires": time.time() * 1000 + 60000})
    async def endpoint(request):
        seen.append(request.headers["Authorization"])
        if len(seen) == 1:
            if trigger == "401":
                return httpx.Response(401, json={"error": {"message": "Rejected fixture credential"}})
            storage.set("modern-oauth", {**storage.get("modern-oauth"), "expires": 1})
            return response(tool=True)
        return response()
    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as client:
        fixture = await fixture_for(tmp_path, monkeypatch, runtime, "modern-oauth", client)
        state, _ = await run_query(fixture, tmp_path)
        assert state.terminal_status == "completed"
        assert seen == ["Bearer fixture-old", "Bearer fixture-new"]
        assert rotations == ["fixture-old"]
        await fixture.adapters[0].aclose()


@pytest.mark.asyncio
async def test_auth_refresh_preserves_admitted_contract_and_newer_selection(tmp_path, monkeypatch):
    version, seen = 0, []
    async def resolve(_input):
        return {"auth": {"api_key": f"fixture-{version}"}}
    runtime, _ = _modern_api_key_runtime({"resolve": resolve})
    async def endpoint(request):
        nonlocal version
        seen.append(json.loads(request.content))
        if len(seen) == 1:
            version = 1
            provider = dict(runtime._extension_providers["modern-auth"])
            provider["models"] = [{**provider["models"][0], "context_window": 64000,
                "instructions": "New catalog instructions must wait for a new model selection"}]
            runtime.register_provider("modern-auth", provider)
            return httpx.Response(401, json={"error": {"message": "Rejected fixture credential"}})
        assert fixture.owner.model_execution is newer
        assert fixture.owner.active_model_execution.llm is fixture.adapters[1]
        return response()
    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as client:
        fixture = await fixture_for(tmp_path, monkeypatch, runtime, "modern-auth", client)
        original = fixture.owner.model_execution
        newer = replace(original, thinking_level="high")
        async def refresh(snapshot, force, task):
            result = await _refresh_session_model_auth(fixture.host, snapshot, force, task)
            if force:
                fixture.owner.model_execution = newer
            return result
        fixture.owner.refresh_model_auth = refresh
        state, _ = await run_query(fixture, tmp_path)
        assert state.terminal_status == "completed"
        assert seen[0] == seen[1]
        assert fixture.adapters[1].provider_adapter_spec.context_window == original.llm.provider_adapter_spec.context_window
        assert fixture.owner.model_execution is newer
        await fixture.adapters[0].aclose()
