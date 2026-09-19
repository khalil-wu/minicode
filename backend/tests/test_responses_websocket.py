from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace

import httpx
import pytest
from websockets.asyncio.server import serve
from websockets.datastructures import Headers
from websockets.http11 import Response

from backend.config import LLMSettings
from backend.agent.provider_protocol import _safe_provider_request_summary
from backend.llm.base import LLMMessage, StreamEventType
from backend.llm.openai_adapter import OpenAIAdapter
from backend.tests.test_committed_tool_scheduling import WorkTool, run_case
from backend.tests.test_harness_native_tool_protocol import PATCH, custom_item
from backend.tools.apply_patch import ApplyPatchTool


def terminal(response_id, *, output=None, text="Finished."):
    return {"type": "response.completed", "response": {
        "id": response_id, "status": "completed",
        "output": output if output is not None else [{"type": "message", "role": "assistant", "id": "msg-" + response_id,
            "status": "completed", "content": [{"type": "output_text", "text": text, "annotations": []}]}],
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }}


def function_call():
    return {"type": "function_call", "id": "fc-1", "call_id": "call-1", "name": "inspect", "arguments": "{}", "status": "completed"}


async def collect(adapter, messages, *, owner="thread", tools=None):
    return [event async for event in adapter.stream_chat(messages, tools, metadata={"thread_id": owner, "turn_id": "turn"})]


def test_chat_transport_is_not_switched_by_an_unused_websocket_setting():
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200))) as client:
            adapter = OpenAIAdapter(LLMSettings(api_key="fixture", model="fixture-model",
                wire_api="chat", responses_websocket=True), http_client=client)
            assert not adapter.try_fallback_transport()
            await adapter.aclose()
    asyncio.run(scenario())


def test_responses_http_replays_sticky_turn_state_only_within_one_turn():
    async def scenario():
        requests = []

        def reply(request):
            requests.append(dict(request.headers))
            body = "data: " + json.dumps(terminal(str(len(requests)))) + "\n\ndata: [DONE]\n\n"
            return httpx.Response(
                200,
                headers={
                    "content-type": "text/event-stream",
                    "x-codex-turn-state": "sticky-fixture",
                },
                text=body,
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            adapter = OpenAIAdapter(
                LLMSettings(
                    api_key="fixture", provider="openai", model="fixture-model",
                    base_url="https://fixture.invalid/v1", wire_api="responses",
                    responses_websocket=False,
                ),
                http_client=client,
            )
            try:
                metadata = {"session_id": "session", "thread_id": "thread", "turn_id": "turn-1"}
                await collect(adapter, [LLMMessage(role="user", content="first")], owner="thread")
                await collect(adapter, [LLMMessage(role="user", content="second")], owner="thread")
                # collect() supplies a stable turn_id; the next explicit call
                # uses a different owner/turn and must not inherit the token.
                await collect(
                    adapter,
                    [LLMMessage(role="user", content="new turn")],
                    owner="thread-new",
                )
                assert "x-codex-turn-state" not in requests[0]
                assert requests[1]["x-codex-turn-state"] == "sticky-fixture"
                assert "x-codex-turn-state" not in requests[2]
            finally:
                await adapter.aclose()

    asyncio.run(scenario())


def test_last_retry_switches_repeated_websocket_failure_to_http(tmp_path, monkeypatch):
    monkeypatch.setenv("MINICODE_STATE_ROOT", str(tmp_path / "state"))

    async def scenario():
        http_requests = []

        async def disconnected(socket, request, index):
            socket.transport.close()

        def http_reply(request):
            http_requests.append(json.loads(request.content))
            body = "data: " + json.dumps(terminal("http-success")) + "\n\n"
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

        async with httpx.AsyncClient(transport=httpx.MockTransport(http_reply)) as client:
            async with endpoint(disconnected, http_client=client) as (adapter, requests, connections):
                state, _, events = await run_case(tmp_path, adapter, [], stream_max_attempts=2)
                assert state.terminal_status == "completed"
                assert len(requests) == 2
                assert len(http_requests) == 1
                assert "previous_response_id" not in http_requests[0]
                assert any("HTTPS" in str(event.data) for event in events)
                assert adapter._responses_websocket.http_only

    asyncio.run(scenario())


@asynccontextmanager
async def endpoint(responder, *, http_client=None, process_request=None):
    requests, connections = [], []
    async def handler(socket):
        connections.append(socket)
        async for raw in socket:
            request = json.loads(raw)
            requests.append(request)
            await responder(socket, request, len(requests))
    async with serve(handler, "127.0.0.1", 0, max_size=None, close_timeout=1, process_request=process_request) as server:
        port = server.sockets[0].getsockname()[1]
        adapter = OpenAIAdapter(LLMSettings(api_key="fixture", provider="openai", model="fixture-model",
            base_url=f"http://127.0.0.1:{port}/v1", wire_api="responses", proxy_mode="direct", responses_websocket=True), http_client=http_client)
        try:
            yield adapter, requests, connections
        finally:
            await adapter.aclose()


@pytest.mark.parametrize("native", [False, True])
def test_query_engine_reuses_connection_and_sends_only_tool_result(tmp_path, monkeypatch, native):
    monkeypatch.setenv("MINICODE_STATE_ROOT", str(tmp_path / "state"))
    async def scenario():
        async def respond(socket, request, index):
            output = [custom_item() if native else function_call()] if index == 1 else None
            await socket.send(json.dumps(terminal(f"response-{index}", output=output)))
        async with endpoint(respond) as (adapter, requests, connections):
            adapter._settings = replace(adapter._settings, supports_custom_tools=native)
            tool = ApplyPatchTool() if native else WorkTool("inspect")
            state, _, events = await run_case(tmp_path, adapter, [tool])
            assert state.terminal_status == "completed", state.stopped_reason
            assert len(requests) == 2 and len(connections) == 1
            assert requests[1]["previous_response_id"] == "response-1"
            assert len(requests[1]["input"]) == 1
            assert requests[1]["input"][0]["type"] == ("custom_tool_call_output" if native else "function_call_output")
            assert len(state.tool_calls) == 1
            if native:
                assert (tmp_path / "result.txt").read_text(encoding="utf-8").startswith("中文 😀")
            else:
                assert tool.executions == 1
            assert not adapter._responses_websocket.active
            assert adapter._responses_websocket.idle is not None
            traces = [event.data.get("provider_raw", {}) for event in events if isinstance(event.data, dict)]
            assert any(trace.get("request_summary", {}).get("transport", {}).get("incremental") for trace in traces)
    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["append", "history", "instructions", "model", "owner", "headers", "tools"])
def test_delta_requires_matching_context_request_and_owner(change):
    async def scenario():
        async def respond(socket, request, index):
            await socket.send(json.dumps(terminal(str(index), text="answer")))
        async with endpoint(respond) as (adapter, requests, connections):
            messages = [LLMMessage(role="system", content="instructions"), LLMMessage(role="user", content="private prompt")]
            await collect(adapter, messages)
            second = [*messages, LLMMessage(role="assistant", content="answer"), LLMMessage(role="user", content="next")]
            if change == "history": second[1] = LLMMessage(role="user", content="edited prompt")
            if change == "instructions": second[0] = LLMMessage(role="system", content="different instructions")
            if change == "model": adapter._settings = replace(adapter._settings, model="other-model")
            if change == "headers": adapter._default_headers["X-Route"] = "different"
            emitted = await collect(adapter, second, owner="other" if change == "owner" else "thread",
                                    tools=[WorkTool("inspect").get_schema().to_openai_tool()] if change == "tools" else None)
            assert ("previous_response_id" in requests[1]) == (change == "append")
            assert len(connections) == (2 if change in {"owner", "headers"} else 1)
            done = next(event for event in emitted if event.type == StreamEventType.DONE)
            summary = done.raw["request_summary"]
            assert summary["transport"]["mode"] == "websocket"
            assert summary["input_items_logical_len"] == 3
            assert summary["input_items_sent_len"] == (1 if change == "append" else 3)
            assert "private prompt" not in json.dumps(done.raw)
    asyncio.run(scenario())


def test_concurrent_requests_have_separate_sockets_without_serializing():
    async def scenario():
        arrived = 0
        ready = asyncio.Event()
        async def respond(socket, request, index):
            nonlocal arrived
            arrived += 1
            if arrived == 2: ready.set()
            await asyncio.wait_for(ready.wait(), 2)
            await socket.send(json.dumps(terminal(str(index), text=request["input"][0]["content"])))
        async with endpoint(respond) as (adapter, requests, connections):
            a, b = await asyncio.gather(
                collect(adapter, [LLMMessage(role="user", content="a")], owner="a"),
                collect(adapter, [LLMMessage(role="user", content="b")], owner="b"),
            )
            assert "".join(event.content for event in a if event.type == StreamEventType.TEXT_CHUNK) == "a"
            assert "".join(event.content for event in b if event.type == StreamEventType.TEXT_CHUNK) == "b"
            assert len(connections) == 2
            assert not adapter._responses_websocket.active
            assert adapter._responses_websocket.idle is not None
    asyncio.run(scenario())


@pytest.mark.parametrize("cancel_task", [False, True])
def test_partial_stream_close_and_cancellation_release_connection(cancel_task):
    async def scenario():
        began, closed = asyncio.Event(), asyncio.Event()
        async def respond(socket, request, index):
            if index == 1:
                await socket.send(json.dumps({"type": "response.output_text.delta", "delta": "partial", "item_id": "msg"}))
                began.set()
                await socket.wait_closed()
                closed.set()
            else:
                await socket.send(json.dumps(terminal("fresh")))
        async with endpoint(respond) as (adapter, requests, connections):
            messages = [LLMMessage(role="user", content="work")]
            if cancel_task:
                task = asyncio.create_task(collect(adapter, messages))
                await asyncio.wait_for(began.wait(), 2)
                task.cancel()
                with pytest.raises(asyncio.CancelledError): await task
            else:
                stream = adapter.stream_chat(messages, metadata={"thread_id": "thread", "turn_id": "turn"})
                first = await anext(stream)
                assert first.content == "partial"
                await stream.aclose()
            await asyncio.wait_for(closed.wait(), 2)
            assert not adapter._responses_websocket.active and adapter._responses_websocket.idle is None
            await collect(adapter, messages)
            assert len(connections) == 2 and "previous_response_id" not in requests[1]
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["disconnect", "expired_id", "expired_id_flat", "expired_id_failed", "committed_disconnect"])
def test_existing_harness_recovery_uses_full_context_and_never_repeats_tools(tmp_path, monkeypatch, failure):
    monkeypatch.setenv("MINICODE_STATE_ROOT", str(tmp_path / "state"))
    async def scenario():
        async def respond(socket, request, index):
            if index == 1:
                if failure == "disconnect":
                    await socket.send(json.dumps({"type": "response.output_text.delta", "item_id": "msg", "delta": "discard me"}))
                    socket.transport.close()
                elif failure == "committed_disconnect":
                    await socket.send(json.dumps({"type": "response.output_item.done", "item": function_call()}))
                    socket.transport.close()
                else:
                    await socket.send(json.dumps(terminal("original", output=[function_call()])))
            elif failure.startswith("expired_id") and index == 2:
                assert request["previous_response_id"] == "original"
                error = {"code": "previous_response_id_not_found", "message": "Not cached"}
                event = {"type": "error", "error": error}
                if failure == "expired_id_flat": event = {"type": "error", **error}
                if failure == "expired_id_failed": event = {"type": "response.failed", "response": {"status": "failed", "error": error}}
                await socket.send(json.dumps(event))
            else:
                assert "previous_response_id" not in request
                await socket.send(json.dumps(terminal("recovered", text="Correct final answer.")))
        async with endpoint(respond) as (adapter, requests, connections):
            tool = WorkTool("inspect")
            state, _, _ = await run_case(tmp_path, adapter, [tool])
            assert state.terminal_status == "completed", state.stopped_reason
            assert "Correct final answer." in state.reply
            assert "discard me" not in state.reply
            assert tool.executions == (0 if failure == "disconnect" else 1)
            assert len(requests) == (3 if failure.startswith("expired_id") else 2)
    asyncio.run(scenario())


@pytest.mark.parametrize("status", [426, 401])
def test_upgrade_rejection_falls_back_only_before_generation_and_runs_hooks_once(monkeypatch, status):
    async def scenario():
        handshakes, http_requests, hook_inputs = [], [], []
        def reject(socket, request):
            handshakes.append(request)
            return Response(status, "Rejected", Headers(), b"")
        async def http(request):
            payload = json.loads(request.content)
            http_requests.append(payload)
            return httpx.Response(200, content="data: " + json.dumps(terminal("http")) + "\n\n", headers={"content-type": "text/event-stream"})
        async def hook(metadata, payload):
            hook_inputs.append(payload)
            return {**payload, "instructions": "hook instructions"}
        monkeypatch.setattr("backend.llm.openai_adapter.emit_provider_lifecycle_request", hook)
        async with httpx.AsyncClient(transport=httpx.MockTransport(http)) as client:
            async with endpoint(lambda *_: None, http_client=client, process_request=reject) as (adapter, requests, connections):
                emitted = await collect(adapter, [LLMMessage(role="user", content="hello")])
                if status == 401:
                    error = next(event for event in emitted if event.type == StreamEventType.ERROR)
                    assert error.raw["status_code"] == 401
                    assert not http_requests
                else:
                    await collect(adapter, [LLMMessage(role="user", content="second")])
                    assert len(handshakes) == 1 and len(http_requests) == len(hook_inputs) == 2
                    assert all(request["instructions"] == "hook instructions" for request in http_requests)
                    assert all("previous_response_id" not in request for request in http_requests)
                    assert emitted[-1].raw["request_summary"]["transport"]["mode"] == "http"
                assert not adapter._responses_websocket.active
            assert not client.is_closed
    asyncio.run(scenario())


def test_side_query_does_not_replace_main_connection_or_baseline():
    async def scenario():
        async def respond(socket, request, index):
            await socket.send(json.dumps(terminal(str(index), text="main")))
        async def http(request):
            payload = json.loads(request.content)
            result = terminal("side", text="summary")
            if payload.get("stream"):
                return httpx.Response(200, content="data: " + json.dumps(result) + "\n\n")
            return httpx.Response(200, json=result["response"])
        async with httpx.AsyncClient(transport=httpx.MockTransport(http)) as client:
            async with endpoint(respond, http_client=client) as (adapter, requests, connections):
                messages = [LLMMessage(role="user", content="work")]
                await collect(adapter, messages)
                assert await adapter.simple_chat([LLMMessage(role="user", content="summarize")]) == "summary"
                await collect(adapter, [*messages, LLMMessage(role="assistant", content="main"), LLMMessage(role="user", content="continue")])
                assert requests[1]["previous_response_id"] == "1"
                assert len(connections) == 1
    asyncio.run(scenario())


def test_protocol_rejection_cannot_publish_a_replay_baseline():
    async def scenario():
        async def respond(socket, request, index):
            if index == 1:
                await socket.send(json.dumps({"type": "response.function_call_arguments.done", "item_id": "fc-1",
                                              "call_id": "call-1", "name": "inspect", "arguments": "{}"}))
                await socket.send(json.dumps(terminal("bad", output=[{**function_call(), "arguments": '{"changed":true}'}])))
            else:
                await socket.send(json.dumps(terminal("good")))
        async with endpoint(respond) as (adapter, requests, connections):
            messages = [LLMMessage(role="user", content="work")]
            events = await collect(adapter, messages)
            assert any(event.type == StreamEventType.ERROR for event in events)
            assert adapter._responses_websocket.idle is None
            await collect(adapter, messages)
            assert len(connections) == 2 and "previous_response_id" not in requests[1]
    asyncio.run(scenario())


def test_large_opaque_reasoning_frames_round_trip_without_truncation():
    async def scenario():
        reasoning = {"type": "reasoning", "id": "reason", "encrypted_content": "z" * 1_100_000, "summary": []}
        async def respond(socket, request, index):
            await socket.send(json.dumps(terminal(str(index), output=[reasoning])))
        async with endpoint(respond) as (adapter, requests, connections):
            messages = [LLMMessage(role="user", content="work")]
            events = await collect(adapter, messages)
            done = next(event for event in events if event.type == StreamEventType.DONE)
            assert done.provider_items[0]["encrypted_content"] == reasoning["encrypted_content"]
            await collect(adapter, [*messages, LLMMessage(role="assistant", provider_items=done.provider_items), LLMMessage(role="user", content="next")])
            assert requests[1]["previous_response_id"] == "1"
            assert requests[1]["input"] == [{"role": "user", "content": "next"}]
    asyncio.run(scenario())


def test_adapter_close_during_handshake_prevents_prompt_send():
    async def scenario():
        began, release = asyncio.Event(), asyncio.Event()
        async def handshake(socket, request):
            began.set()
            await release.wait()
        async with endpoint(lambda *_: None, process_request=handshake) as (adapter, requests, connections):
            task = asyncio.create_task(collect(adapter, [LLMMessage(role="user", content="never send")]))
            await asyncio.wait_for(began.wait(), 2)
            await adapter.aclose()
            release.set()
            events = await asyncio.wait_for(task, 2)
            assert any(event.type == StreamEventType.ERROR for event in events)
            assert not requests and not adapter._responses_websocket.active
    asyncio.run(scenario())


def test_transport_projection_keeps_metrics_without_connection_credentials():
    transport = {"mode": "websocket", "incremental": True, "connection_reused": True,
                 "input_items_logical_len": 2003, "input_items_sent_len": 1, "request_json_bytes": 850,
                 "headers": {"Authorization": "secret"}, "previous_response_id": "private-id"}
    projected = _safe_provider_request_summary({"transport": transport})["transport"]
    assert projected == {key: value for key, value in transport.items() if key not in {"headers", "previous_response_id"}}


def test_streamed_text_missing_from_terminal_items_forces_full_replay():
    async def scenario():
        async def respond(socket, request, index):
            if index == 1:
                await socket.send(json.dumps({"type": "response.output_text.delta", "item_id": "msg", "delta": "answer"}))
                await socket.send(json.dumps(terminal("incomplete-items", output=[])))
            else:
                await socket.send(json.dumps(terminal("complete-items")))
        async with endpoint(respond) as (adapter, requests, connections):
            messages = [LLMMessage(role="user", content="question")]
            events = await collect(adapter, messages)
            assert events[-1].type == StreamEventType.DONE
            assert "".join(event.content for event in events if event.type == StreamEventType.TEXT_CHUNK) == "answer"
            assert events[-1].raw["request_summary"]["transport"]["response_items_complete"] is False
            assert adapter._responses_websocket.idle is None
            await collect(adapter, [*messages, LLMMessage(role="assistant", content="answer"), LLMMessage(role="user", content="continue")])
            assert "previous_response_id" not in requests[1]
            assert len(requests[1]["input"]) == 3
    asyncio.run(scenario())


def test_diagnostics_summarize_only_actual_sent_input_after_request_hooks(monkeypatch):
    import backend.llm.openai_adapter as module
    sizes = []
    summarize = module._safe_request_summary
    def measured(**kwargs):
        sizes.append(len(kwargs["input_items"]))
        return summarize(**kwargs)
    async def hook(metadata, payload):
        return {**payload, "model": "effective-model"}
    monkeypatch.setattr(module, "_safe_request_summary", measured)
    monkeypatch.setattr(module, "emit_provider_lifecycle_request", hook)
    async def scenario():
        async def respond(socket, request, index):
            await socket.send(json.dumps(terminal(str(index), text="answer")))
        async with endpoint(respond) as (adapter, requests, connections):
            messages = [LLMMessage(role="user", content="x" * 2000) for _ in range(20)]
            await collect(adapter, messages)
            events = await collect(adapter, [*messages, LLMMessage(role="assistant", content="answer"), LLMMessage(role="user", content="continue")])
            assert sizes == [20, 1]
            assert events[-1].raw["model"] == "effective-model"
            assert events[-1].raw["request_summary"]["model"] == "effective-model"
    asyncio.run(scenario())


@pytest.mark.parametrize("control", ["previous_response_id", "conversation"])
def test_hook_owned_continuation_control_is_not_overridden(monkeypatch, control):
    async def hook(metadata, payload):
        return {**payload, control: "host-owned"}
    monkeypatch.setattr("backend.llm.openai_adapter.emit_provider_lifecycle_request", hook)
    async def scenario():
        async def respond(socket, request, index):
            await socket.send(json.dumps(terminal(str(index), text="answer")))
        async with endpoint(respond) as (adapter, requests, connections):
            messages = [LLMMessage(role="user", content="question")]
            await collect(adapter, messages)
            await collect(adapter, [*messages, LLMMessage(role="assistant", content="answer"), LLMMessage(role="user", content="continue")])
            assert requests[1][control] == "host-owned"
            assert len(requests[1]["input"]) == 3
            if control == "conversation":
                assert "previous_response_id" not in requests[1]
    asyncio.run(scenario())
