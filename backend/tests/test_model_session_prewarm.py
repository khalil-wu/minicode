"""Preconnect and generate=false warmup through real Responses sockets."""
import asyncio
from dataclasses import replace
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from websockets.datastructures import Headers
from websockets.http11 import Response

from backend.llm.base import LLMMessage, StreamEventType
from backend.tests.test_responses_websocket import endpoint, terminal
from backend.tests.test_committed_tool_scheduling import run_case, WorkTool
from backend.ws.model_prewarm import schedule_model_prewarm


def metadata(turn="actual"):
    return {"session_id": "session", "thread_id": "thread", "conversation_id": "thread", "turn_id": turn}


async def completed(adapter, messages, *, owner=None, tools=None):
    events = [event async for event in adapter.stream_chat(messages, tools, metadata=owner or metadata())]
    assert events[-1].type == StreamEventType.DONE, events
    return events


@pytest.mark.asyncio
async def test_preconnect_sends_no_prompt_and_actual_turn_reuses_its_socket():
    async def respond(socket, request, index):
        await socket.send(json.dumps(terminal(str(index))))
    async with endpoint(respond) as (adapter, requests, connections):
        await adapter.schedule_session_preconnect(metadata("startup-prewarm"))
        assert len(connections) == 1 and requests == []
        events = await completed(adapter, [LLMMessage("user", "Actual request")])
        assert len(connections) == 1 and len(requests) == 1
        assert "generate" not in requests[0]
        assert events[-1].raw["request_summary"]["transport"]["connection_reused"]
        assert not adapter._session_warmups and not adapter._session_warmup_tasks


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["none", "history", "tools", "credentials", "route"])
async def test_warmup_uses_canonical_payload_and_only_matching_context_continues(change):
    async def respond(socket, request, index):
        await socket.send(json.dumps(terminal(str(index), output=[] if request.get("generate") is False else None)))
    async with endpoint(respond) as (adapter, requests, connections):
        messages = [LLMMessage("system", "Actual instructions"), LLMMessage("user", "Saved history")]
        tools = [WorkTool("inspect").get_schema().to_openai_tool()]
        await adapter.schedule_session_preconnect(metadata("startup-prewarm"))
        await adapter.schedule_session_prewarm(messages, tools, metadata())
        assert len(connections) == 1 and requests[0]["generate"] is False
        assert requests[0]["instructions"] == "Actual instructions" and requests[0]["tools"]
        if change == "history":
            messages[1] = LLMMessage("user", "External replacement")
        if change == "tools":
            tools = []
        if change == "credentials":
            adapter._settings = replace(adapter._settings, api_key="rotated-key")
        if change == "route":
            adapter._default_headers["X-Route"] = "new-route"
        await completed(adapter, [*messages, LLMMessage("user", "New input")], tools=tools)
        assert ("previous_response_id" in requests[1]) == (change == "none")
        assert len(connections) == (2 if change in {"credentials", "route"} else 1)
        assert "generate" not in requests[1]
        if change == "none":
            assert requests[1]["input"] == [{"role": "user", "content": "New input"}]
        assert not adapter._responses_websocket.active


@pytest.mark.asyncio
async def test_real_request_waits_for_pending_warmup_and_does_not_share_recv():
    entered, release = asyncio.Event(), asyncio.Event()
    async def respond(socket, request, index):
        if request.get("generate") is False:
            entered.set()
            await release.wait()
        await socket.send(json.dumps(terminal(str(index), output=[])))
    async with endpoint(respond) as (adapter, requests, connections):
        warm = adapter.schedule_session_prewarm([], [], metadata())
        await asyncio.wait_for(entered.wait(), 3)
        actual = asyncio.create_task(completed(adapter, [LLMMessage("user", "Generate")]))
        await asyncio.sleep(.03)
        assert len(requests) == 1 and not actual.done()
        release.set()
        await actual
        assert warm.done() and len(connections) == 1 and len(requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["cancel", "timeout", "close"])
async def test_cancel_timeout_and_close_release_the_exact_warmup_socket(stop):
    entered = asyncio.Event()
    async def respond(socket, request, index):
        if request.get("generate") is False:
            entered.set()
            await socket.wait_closed()
        else:
            await socket.send(json.dumps(terminal(str(index))))
    async with endpoint(respond) as (adapter, requests, connections):
        warm = adapter.schedule_session_prewarm([], [], metadata())
        await asyncio.wait_for(entered.wait(), 3)
        if stop == "close":
            await adapter.aclose()
        elif stop == "cancel":
            actual = asyncio.create_task(completed(adapter, [LLMMessage("user", "Cancelled input")]))
            await asyncio.sleep(.03)
            actual.cancel()
            with pytest.raises(asyncio.CancelledError):
                await actual
        else:
            key = adapter._warmup_key(adapter._responses_turn_owner(metadata()))
            task, _started, kind = adapter._session_warmups[key]
            adapter._session_warmups[key] = (task, time.monotonic() - 20, kind)
            await completed(adapter, [LLMMessage("user", "After warmup timeout")])
            assert len(connections) == 2 and len(requests) == 2
        assert warm.done() and not adapter._responses_websocket.active
        await asyncio.wait_for(connections[0].wait_closed(), 2)


@pytest.mark.asyncio
async def test_rejected_websocket_warmup_never_falls_back_to_http_generation():
    http_calls = []
    def http(request):
        http_calls.append(request)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text="data: " + json.dumps(terminal("real-http")) + "\n\n")
    def reject(_socket, _request):
        return Response(404, "Unsupported", Headers(), b"")
    async with httpx.AsyncClient(transport=httpx.MockTransport(http)) as client:
        async with endpoint(None, process_request=reject, http_client=client) as (adapter, requests, _):
            await adapter.schedule_session_prewarm([], [], metadata())
            assert not http_calls and not requests
            await completed(adapter, [LLMMessage("user", "Actual HTTP input")])
            assert len(http_calls) == 1 and "generate" not in json.loads(http_calls[0].content)


@pytest.mark.asyncio
async def test_two_threads_do_not_consume_each_others_pending_warmup():
    arrived, release = asyncio.Event(), asyncio.Event()
    async def respond(socket, request, index):
        if request.get("generate") is False:
            arrived.set()
            await release.wait()
        await socket.send(json.dumps(terminal(str(index), output=[])))
    async with endpoint(respond) as (adapter, requests, connections):
        warm = adapter.schedule_session_prewarm([LLMMessage("user", "Private A")], [], metadata())
        await asyncio.wait_for(arrived.wait(), 3)
        await completed(adapter, [LLMMessage("user", "Public B")], owner={**metadata(), "thread_id": "other"})
        assert len(connections) == 2 and "Private A" not in json.dumps(requests[1])
        release.set()
        await warm


@pytest.mark.asyncio
async def test_real_query_prewarms_before_admitting_input_without_emitting_a_fake_reply(tmp_path):
    async def respond(socket, request, index):
        await socket.send(json.dumps(terminal(str(index), output=[] if request.get("generate") is False else None)))
    async with endpoint(respond) as (adapter, requests, connections):
        state, _, events = await run_case(tmp_path, adapter, [])
        assert state.terminal_status == "completed"
        assert len(requests) == 2 and requests[0]["generate"] is False
        assert len(connections) == 1
        assert not requests[0]["input"] and requests[1]["input"]
        assert sum(event.type == "done" for event in events) == 1


@pytest.mark.asyncio
async def test_selected_thread_startup_uses_owned_adapter_and_no_user_generation(tmp_path, monkeypatch):
    from backend.tests.test_user_input_presentation_chain import session_for
    async def respond(socket, request, index):
        await socket.send(json.dumps(terminal(str(index))))
    async with endpoint(respond) as (adapter, requests, connections):
        session, record, _ = session_for(tmp_path, monkeypatch)
        session.provider, session.selected_model = "openai", "fixture-model"
        session.config = replace(session.config, llm=adapter._settings)
        monkeypatch.setattr("backend.config.load_config", lambda **_: session.config)
        monkeypatch.setattr("backend.ws.agent_runner._get_or_create_session_llm", lambda *_, **__: adapter)
        try:
            schedule_model_prewarm(session, record.id)
            pending = session._model_prewarm_tasks[record.id][1]
            schedule_model_prewarm(session, record.id)
            assert session._model_prewarm_tasks[record.id][1] is pending
            await pending
            assert len(connections) == 1 and not requests
            await completed(adapter, [LLMMessage("user", "First actual turn")], owner={"session_id": record.id,
                "conversation_id": record.id, "turn_id": "actual"})
            assert len(connections) == 1 and len(requests) == 1
        finally:
            await session.session_lifecycle.shutdown(reason="prewarm-test")


@pytest.mark.asyncio
async def test_changed_selection_replaces_pending_auth_without_publishing_the_old_adapter(tmp_path, monkeypatch):
    from backend.tests.test_user_input_presentation_chain import session_for

    session, record, adapter = session_for(tmp_path, monkeypatch)
    session.provider, session.selected_model = "custom", "old-model"
    session.config = replace(session.config, llm=replace(session.config.llm, model="old-model"))
    entered = asyncio.Event()
    auth_calls = []
    async def refresh_auth(_provider):
        auth_calls.append(True)
        if len(auth_calls) == 1:
            entered.set()
            await asyncio.Event().wait()
    runtime = SimpleNamespace(refresh=lambda **_: None, refresh_oauth_credentials=AsyncMock(),
        refresh_provider_auth=refresh_auth, get_model=lambda *_: None)
    monkeypatch.setattr(session, "_model_runtime_for_conversation", lambda _: runtime)
    monkeypatch.setattr("backend.config.load_config", lambda **_: session.config)
    monkeypatch.setattr("backend.ws.agent_runner._config_with_runtime_model_budget", lambda config, **_: config)
    built = []
    def construct(_session, **options):
        built.append(options["model"])
        return adapter
    monkeypatch.setattr("backend.ws.agent_runner._get_or_create_session_llm", construct)
    try:
        schedule_model_prewarm(session, record.id)
        old = session._model_prewarm_tasks[record.id][1]
        await asyncio.wait_for(entered.wait(), 3)
        session.selected_model = "new-model"
        session.config = replace(session.config, llm=replace(session.config.llm, model="new-model"))
        schedule_model_prewarm(session, record.id)
        new = session._model_prewarm_tasks[record.id][1]
        assert new is not old
        await new
        assert old.cancelled() and built == ["new-model"]
    finally:
        await session.session_lifecycle.shutdown(reason="prewarm-selection-change")


@pytest.mark.asyncio
async def test_prompt_prewarm_waits_for_startup_handshake_then_actual_turn_reuses_it():
    entered, release = asyncio.Event(), asyncio.Event()
    async def handshake(_socket, _request):
        entered.set()
        await release.wait()
    async def respond(socket, request, index):
        await socket.send(json.dumps(terminal(str(index), output=[])))
    async with endpoint(respond, process_request=handshake) as (adapter, requests, connections):
        connect = adapter.schedule_session_preconnect(metadata("startup-prewarm"))
        await asyncio.wait_for(entered.wait(), 3)
        warm = adapter.schedule_session_prewarm([], [], metadata())
        actual = asyncio.create_task(completed(adapter, [LLMMessage("user", "Actual input")]))
        assert requests == []
        release.set()
        await actual
        assert connect.done() and warm.done() and len(connections) == 1 and len(requests) == 2


def test_adapter_cache_does_not_reuse_a_transport_after_websocket_setting_changes():
    from backend.config import AppConfig, LLMSettings
    from backend.ws.agent_runner import _llm_adapter_cache_key

    config = AppConfig(llm=LLMSettings(api_key="fixture", model="fixture", wire_api="responses"))
    first = _llm_adapter_cache_key(config=config, provider="custom", model="fixture")
    second = _llm_adapter_cache_key(config=replace(config, llm=replace(config.llm, responses_websocket=True)),
        provider="custom", model="fixture")
    assert first != second


@pytest.mark.asyncio
async def test_loaded_threads_keep_separate_idle_connections_and_release_only_the_unloaded_thread():
    async def respond(socket, request, index):
        await socket.send(json.dumps(terminal(str(index), output=[] if request.get("generate") is False else None, text="answer")))
    async with endpoint(respond) as (adapter, requests, connections):
        histories = {}
        for name in ("first", "second"):
            owner = {"session_id": name, "thread_id": name, "turn_id": "one"}
            histories[name] = [LLMMessage("user", f"Private {name}")]
            await completed(adapter, histories[name], owner=owner)
        assert len(connections) == 2 and len(adapter._responses_websocket.idle) == 2
        histories["first"].extend([LLMMessage("assistant", "answer"), LLMMessage("user", "Next first")])
        owner = {"session_id": "first", "thread_id": "first", "turn_id": "two"}
        assert not adapter.session_prewarm_needed(owner)
        await completed(adapter, histories["first"], owner=owner)
        assert len(connections) == 2 and requests[2]["previous_response_id"] == "1"
        assert requests[2]["input"] == [{"role": "user", "content": "Next first"}]
        await adapter.close_thread_session("first")
        await asyncio.wait_for(connections[0].wait_closed(), 2)
        assert len(adapter._responses_websocket.idle) == 1 and connections[1].close_code is None
        await completed(adapter, [LLMMessage("user", "Reopened first")], owner=owner)
        assert len(connections) == 3 and "previous_response_id" not in requests[3]


@pytest.mark.asyncio
async def test_unstarted_thread_preconnections_do_not_accumulate():
    async with endpoint(None) as (adapter, requests, connections):
        for name in ("first", "second", "third"):
            await adapter.schedule_session_preconnect({"session_id": name, "thread_id": name, "turn_id": "startup"})
        assert not requests and len(adapter._responses_websocket.idle) == 1
        await asyncio.wait_for(asyncio.gather(connections[0].wait_closed(), connections[1].wait_closed()), 2)
        assert connections[2].close_code is None


@pytest.mark.asyncio
async def test_websocket_thread_unload_releases_its_model_session_and_keeps_other_threads(tmp_path, monkeypatch):
    from backend.tests.test_user_input_presentation_chain import session_for

    async def respond(socket, request, index):
        await socket.send(json.dumps(terminal(str(index), text="answer")))
    async with endpoint(respond) as (adapter, requests, connections):
        session, record, _ = session_for(tmp_path, monkeypatch)
        session._llm_adapter_cache[("owned-responses",)] = adapter
        try:
            for name in (record.id, "other-thread"):
                await completed(adapter, [LLMMessage("user", name)], owner={"session_id": name, "thread_id": name, "turn_id": "one"})
            await session.session_lifecycle.unload_conversation(record.id, reason="thread_idle_unload")
            await asyncio.wait_for(connections[0].wait_closed(), 2)
            assert connections[1].close_code is None
            assert list(adapter._responses_websocket.idle) == [("other-thread", "other-thread", "user")]
        finally:
            await session.session_lifecycle.shutdown(reason="model-unload-test")


@pytest.mark.asyncio
async def test_model_adapter_cache_retains_other_threads_and_retires_only_changed_owner(monkeypatch):
    from backend.config import AppConfig, LLMSettings
    from backend.ws.agent_runner import _get_or_create_session_llm, _clear_session_llm_cache

    host = SimpleNamespace()
    created = []
    def build(*_args, **_kwargs):
        adapter = SimpleNamespace(aclose=AsyncMock())
        created.append(adapter)
        return adapter
    monkeypatch.setattr("backend.llm.model_registry.create_session_llm", build)
    config = AppConfig(llm=LLMSettings(api_key="fixture", model="selected"))
    async def select(owner, selected=config):
        return _get_or_create_session_llm(host, config=selected, provider="custom", model="selected", conversation_id=owner)
    first = await asyncio.create_task(select("first"))
    second = await asyncio.create_task(select("second"))
    assert await asyncio.create_task(select("first")) is first and len(created) == 2
    replacement = await asyncio.create_task(select("first", replace(config, llm=replace(config.llm, api_key="rotated"))))
    await asyncio.gather(*host._llm_close_tasks)
    assert replacement is not first
    first.aclose.assert_awaited_once()
    second.aclose.assert_not_awaited()
    _clear_session_llm_cache(host, conversation_id="first")
    await asyncio.gather(*host._llm_close_tasks)
    assert list(host._llm_adapter_cache.values()) == [second]
    replacement.aclose.assert_awaited_once()
    _clear_session_llm_cache(host)
    await asyncio.gather(*host._llm_close_tasks)
    second.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_navigation_retires_a_speculative_adapter_without_a_loaded_core(tmp_path, monkeypatch):
    from backend.tests.test_user_input_presentation_chain import session_for

    session, record, _ = session_for(tmp_path, monkeypatch)
    session._model_prewarm_view_owner = record.id
    old = SimpleNamespace(aclose=AsyncMock())
    session._llm_adapter_cache[("conversation", record.id, ("wire",))] = old
    other = session.conversation_repo.create_conversation(title="New selected thread")
    session.active_conversation_id = other.id
    session.selected_model = "fixture-model"
    entered = asyncio.Event()
    async def read_config(*_args, **_kwargs):
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr("backend.ws.model_prewarm.to_thread_cancel_safe", read_config)
    try:
        schedule_model_prewarm(session, other.id)
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.gather(*session._llm_close_tasks)
        old.aclose.assert_awaited_once()
        assert not any(key[:2] == ("conversation", record.id) for key in session._llm_adapter_cache)
    finally:
        await session.session_lifecycle.shutdown(reason="speculative-prewarm")


@pytest.mark.asyncio
async def test_startup_route_survives_warmup_handoff_but_not_the_next_completed_turn():
    def handshake(_socket, _request, response):
        response.headers["x-codex-turn-state"] = "startup-route"
        return response
    async def respond(socket, request, index):
        await socket.send(json.dumps(terminal(str(index), output=[])))
    async with endpoint(respond, process_response=handshake) as (adapter, requests, connections):
        await adapter.schedule_session_preconnect(metadata("startup-prewarm"))
        await adapter.schedule_session_prewarm([], [], metadata("prepared-prompt"))
        await completed(adapter, [LLMMessage("user", "First actual request")], owner=metadata("first"))
        assert requests[0]["client_metadata"]["x-codex-turn-state"] == "startup-route"
        assert requests[1]["client_metadata"]["x-codex-turn-state"] == "startup-route"
        await completed(adapter, [LLMMessage("user", "Next actual request")], owner=metadata("second"))
        assert "x-codex-turn-state" not in requests[2]["client_metadata"]
        assert len(connections) == 1
