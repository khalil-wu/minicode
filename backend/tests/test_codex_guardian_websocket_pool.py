"""Source Snapshot transport ownership against real local WebSocket/HTTP sockets."""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
from uuid import UUID

import httpx
import pytest
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed
from websockets.protocol import State

from backend.agent.guardian import connection_pool
from backend.agent.guardian.async_sampler import GuardianSamplingError, LunaSampler
from backend.agent.guardian.async_service import GuardianAsyncService
from backend.agent.guardian.retained import GuardianEvidenceOwner
from backend.llm.base import LLMTurnContext
from backend.llm.model_request_policy import ModelRequestPolicy
from backend.llm.openai_adapter import OpenAIAdapter
from backend.sandbox.policy import PermissionProfile
from backend.tests.test_codex_guardian_async import configured_context


def terminal():
    return {"type": "response.completed", "response": {"id": "actual-local-classifier-response",
        "usage": {"input_tokens": 17, "output_tokens": 3, "input_tokens_details": {"cached_tokens": 7}}}}


async def wait_for_state(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(.001)


async def finish(sampler):
    if sampler.tasks:
        await asyncio.gather(*tuple(sampler.tasks), return_exceptions=True)
    await asyncio.sleep(0)


def local_sampler(ctx, url):
    ctx.run_context.llm_turn_context = LLMTurnContext()
    return LunaSampler(ctx.model_execution, conversation_id=ctx.conversation_id,
        llm_factory=lambda cfg, **_: OpenAIAdapter(replace(cfg.llm,
            base_url=url, wire_api="responses", proxy_mode="direct")))


@asynccontextmanager
async def endpoint(respond):
    handshakes, requests, connections = [], [], []
    async def handler(socket):
        connections.append(socket)
        try:
            async for raw in socket:
                body = json.loads(raw)
                requests.append(body)
                await respond(socket, body, len(requests))
        except ConnectionClosed:
            pass
    def handshake(socket, request):
        handshakes.append(dict(request.headers))
    async with serve(handler, "127.0.0.1", 0, process_request=handshake) as server:
        port = server.sockets[0].getsockname()[1]
        yield f"http://127.0.0.1:{port}/v1", handshakes, requests, connections


class Gateway:
    """Route actual POST SSE and selectively stall actual GET upgrades."""
    def __init__(self, ws_url, *, events=None, stall_headers=False, drain=False):
        self.ws_port = int(ws_url.split(":")[2].split("/")[0])
        self.allowed_opens = 0
        self.opens, self.posts, self.disconnects = [], [], []
        self.events = events if events is not None else [{"type": "response.output_text.delta", "delta": "low"}, terminal()]
        self.stall_headers = stall_headers
        self.drain = drain
        self.tasks, self.writers = set(), set()
        self.errors = []

    async def __aenter__(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        self.url = f"http://127.0.0.1:{self.server.sockets[0].getsockname()[1]}/v1"
        return self

    async def __aexit__(self, *_):
        self.server.close()
        await self.server.wait_closed()
        for writer in tuple(self.writers):
            writer.close()
        tasks = tuple(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def relay(self, source, target):
        while chunk := await source.read(65536):
            target.write(chunk)
            await target.drain()
        target.close()

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        self.writers.add(writer)
        post_index = None
        try:
            header = await reader.readuntil(b"\r\n\r\n")
            lines = header.decode().split("\r\n")
            method, path, _ = lines[0].split(" ")
            headers = dict(line.split(": ", 1) for line in lines[1:] if ": " in line)
            if method == "GET":
                self.opens.append(headers)
                if len(self.opens) > self.allowed_opens:
                    await reader.read()
                else:
                    upstream, outgoing = await asyncio.open_connection("127.0.0.1", self.ws_port)
                    self.writers.add(outgoing)
                    try:
                        outgoing.write(header)
                        await outgoing.drain()
                        await asyncio.gather(self.relay(reader, outgoing), self.relay(upstream, writer))
                    finally:
                        outgoing.close()
                        self.writers.discard(outgoing)
            else:
                body = json.loads(await reader.readexactly(int(headers["Content-Length"])))
                self.posts.append({"body": body, "headers": headers, "path": path})
                post_index = len(self.posts)
                if self.stall_headers:
                    await reader.read()
                else:
                    payload = "".join(f"data: {json.dumps(event)}\n\n" for event in self.events).encode()
                    length = b"" if self.drain else f"Content-Length: {len(payload)}\r\n".encode()
                    writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n" + length + b"\r\n" + payload)
                    await writer.drain()
                    if self.drain:
                        await reader.read()
        except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
            pass
        except Exception as error:
            self.errors.append(error)
        finally:
            if post_index is not None:
                self.disconnects.append(post_index)
            writer.close()
            self.writers.discard(writer)
            self.tasks.discard(task)


@pytest.fixture
def small_pool(monkeypatch):
    # Source cfg(test) also prewarms two; the production capacity is checked separately.
    monkeypatch.setattr(connection_pool, "INITIAL_WEBSOCKET_CONNECTIONS", 2)


@pytest.mark.asyncio
async def test_production_prewarm_eight_distinct_threads_no_inference_and_completed_reuse(tmp_path):
    async def respond(socket, body, index):
        await socket.send(json.dumps({"type": "response.output_text.delta", "delta": "low"}))
        await socket.send(json.dumps(terminal()))
    async with endpoint(respond) as (url, headers, requests, sockets):
        ctx, _ = configured_context(tmp_path)
        sampler = local_sampler(ctx, url)
        try:
            await sampler.prewarm(ctx)
            assert len(headers) == 8 and not requests
            assert len({row["thread_id"] for row in headers}) == 8
            assert all(UUID(row["thread_id"]).version == 7 for row in headers)
            assert all(row["openai-beta"] == "responses_websockets=2026-02-06" for row in headers)
            assert all(row["x-codex-window-id"] == row["thread_id"] + ":0" for row in headers)
            assert sampler.connections.sockets._value == 8
            assert sampler.connections.classifications._value == 16
            for _ in range(2):
                assert await sampler.sample([], instructions="source policy", context=ctx) == "low"
                await finish(sampler)
            assert len(headers) == 8 and len(requests) == 2
            assert requests[0]["client_metadata"]["thread_id"] == requests[1]["client_metadata"]["thread_id"]
            assert requests[0]["client_metadata"]["turn_id"] != requests[1]["client_metadata"]["turn_id"]
            assert all(UUID(row["client_metadata"]["turn_id"]).version == 7 for row in requests)
            assert all(row["type"] == "response.create" and "previous_response_id" not in row and "conversation" not in row for row in requests)
            assert len(requests[0]["input"]) == len(requests[1]["input"]) == 2
            assert ctx.run_context.llm_turn_context.usage.input_tokens == 34
            assert ctx.run_context.llm_turn_context.usage.output_tokens == 6
            assert not sampler.connections.active and len(sampler.connections.idle) == 8
        finally:
            await sampler.aclose()
        assert sampler.connections.sockets._value == sampler.connections.classifications._value == 16
        await wait_for_state(lambda: all(socket.state == State.CLOSED for socket in sockets))


@pytest.mark.asyncio
@pytest.mark.parametrize("first", ["high", "low", " LOW", "lo", "low now"])
@pytest.mark.parametrize("late_error", [False, True])
async def test_websocket_first_nonempty_delta_is_exact_and_drain_keeps_error_usage(tmp_path, small_pool, first, late_error):
    release = asyncio.Event()
    async def respond(socket, body, index):
        for delta in ["", first, "high"]:
            await socket.send(json.dumps({"type": "response.output_text.delta", "delta": delta}))
        await release.wait()
        ending = {"type": "response.failed", "response": {"error": {"message": "real late failure"}}} if late_error else terminal()
        await socket.send(json.dumps(ending))
    async with endpoint(respond) as (url, headers, requests, sockets):
        ctx, _ = configured_context(tmp_path)
        sampler = local_sampler(ctx, url)
        try:
            await sampler.prewarm(ctx)
            assert await sampler.sample([], instructions="source policy", context=ctx) == first
            assert len(sampler.connections.active) == 1 and len(sampler.connections.idle) == 1
            assert sampler.connections.classifications._value == 15 and sampler.connections.sockets._value == 14
            release.set()
            await finish(sampler)
            record = ctx.run_context.llm_turn_context.side_call_records[0]
            assert record["transports"] == ["websocket"] and record["attempts"] == 1
            if late_error:
                assert record["status"] == "failed" and "real late failure" in record["error"]
                assert record["provider_error"]["response"]["error"]["message"] == "real late failure"
                assert len(sampler.connections.idle) == 1 and sampler.connections.sockets._value == 15
                assert ctx.run_context.llm_turn_context.usage.input_tokens == 0
            else:
                assert record["status"] == "completed" and len(sampler.connections.idle) == 2
                assert ctx.run_context.llm_turn_context.usage.input_tokens == 17
        finally:
            release.set()
            await sampler.aclose()


@pytest.mark.asyncio
async def test_cold_pool_http_during_open_timeout_cooldown_and_actual_ws_recovery(tmp_path, small_pool):
    async def respond(socket, body, index):
        await socket.send(json.dumps({"type": "response.output_text.delta", "delta": "low"}))
        await socket.send(json.dumps(terminal()))
    async with endpoint(respond) as (url, headers, requests, sockets):
        async with Gateway(url) as gateway:
            ctx, _ = configured_context(tmp_path)
            sampler = local_sampler(ctx, gateway.url)
            clock = [0.0]
            sampler.connections.clock = lambda: clock[0]
            sampler.connections.connect_timeout = .05
            try:
                sampler.connections.replenish(ctx)
                await wait_for_state(lambda: len(gateway.opens) == 1)
                assert await asyncio.wait_for(sampler.sample([], instructions="source policy", context=ctx), .5) == "low"
                await finish(sampler)
                await wait_for_state(lambda: sampler.connections.replenishing is None)
                assert sampler.connections.retry_after == connection_pool.CONNECT_COOLDOWN
                assert sampler.connections.replenish(ctx) is None
                assert await sampler.sample([], instructions="source policy", context=ctx) == "low"
                await finish(sampler)
                assert len(gateway.posts) == 2 and len(gateway.opens) == 1
                gateway.allowed_opens = 100
                clock[0] += connection_pool.CONNECT_COOLDOWN
                await sampler.prewarm(ctx)
                assert await sampler.sample([], instructions="source policy", context=ctx) == "low"
                await finish(sampler)
                assert len(gateway.posts) == 2 and len(requests) == 1 and len(gateway.opens) == 3
                assert sampler.connections.retry_after is None
                assert [row["transports"] for row in ctx.run_context.llm_turn_context.side_call_records] == [["http"], ["http"], ["websocket"]]
                assert not gateway.errors
            finally:
                await sampler.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("published", [False, True])
async def test_websocket_disconnect_uses_one_sampling_budget_and_never_revises_early_score(tmp_path, small_pool, published):
    async def respond(socket, body, index):
        if index == 1:
            if published:
                await socket.send(json.dumps({"type": "response.output_text.delta", "delta": "low"}))
            await socket.close(code=1011)
        else:
            await socket.send(json.dumps({"type": "response.output_text.delta", "delta": "high"}))
            await socket.send(json.dumps(terminal()))
    async with endpoint(respond) as (url, headers, requests, sockets):
        ctx, _ = configured_context(tmp_path)
        sampler = local_sampler(ctx, url)
        try:
            await sampler.prewarm(ctx)
            assert await sampler.sample([], instructions="source policy", context=ctx) == ("low" if published else "high")
            await finish(sampler)
            if published:
                assert len(requests) == 1
                assert ctx.run_context.llm_turn_context.side_call_records[0]["status"] == "failed"
            else:
                assert len(requests) == 2 and requests[0]["input"] == requests[1]["input"]
                assert requests[0]["client_metadata"]["turn_id"] == requests[1]["client_metadata"]["turn_id"]
                assert requests[0]["client_metadata"]["thread_id"] != requests[1]["client_metadata"]["thread_id"]
                assert ctx.run_context.llm_turn_context.side_call_records[0]["attempts"] == 2
            assert sampler.connections.classifications._value == 16
        finally:
            await sampler.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("code,retry", [("server_is_overloaded", True), ("invalid_prompt", False), ("context_length_exceeded", False)])
async def test_source_failed_response_retry_codes_are_shared_by_websocket_sampling(tmp_path, small_pool, code, retry):
    async def respond(socket, body, index):
        if index == 1:
            await socket.send(json.dumps({"type": "response.failed", "response": {"error": {"code": code, "message": "actual first request rejection"}}}))
        else:
            await socket.send(json.dumps({"type": "response.output_text.delta", "delta": "low"}))
            await socket.send(json.dumps(terminal()))
    async with endpoint(respond) as (url, headers, requests, sockets):
        ctx, _ = configured_context(tmp_path)
        sampler = local_sampler(ctx, url)
        try:
            await sampler.prewarm(ctx)
            if retry:
                assert await sampler.sample([], instructions="source policy", context=ctx) == "low"
            else:
                with pytest.raises(GuardianSamplingError, match="actual first request rejection"):
                    await sampler.sample([], instructions="source policy", context=ctx)
            await finish(sampler)
            assert len(requests) == (2 if retry else 1)
            record = ctx.run_context.llm_turn_context.side_call_records[0]
            assert record["provider_error"]["response"]["error"]["code"] == code
            assert record["attempts"] == (2 if retry else 1)
        finally:
            await sampler.aclose()


@pytest.mark.asyncio
async def test_source_interrupted_completion_keeps_real_usage_and_reusable_socket(tmp_path, small_pool):
    async def respond(socket, body, index):
        await socket.send(json.dumps({"type": "response.output_text.delta", "delta": "low"}))
        event = terminal()
        event["type"] = "response.incomplete"
        event["response"]["incomplete_details"] = {"reason": "interrupted"}
        await socket.send(json.dumps(event))
    async with endpoint(respond) as (url, headers, requests, sockets):
        ctx, _ = configured_context(tmp_path)
        sampler = local_sampler(ctx, url)
        try:
            await sampler.prewarm(ctx)
            assert await sampler.sample([], instructions="source policy", context=ctx) == "low"
            await finish(sampler)
            assert ctx.run_context.llm_turn_context.side_call_records[0]["interrupted"] is True
            assert ctx.run_context.llm_turn_context.usage.input_tokens == 17
            assert len(sampler.connections.idle) == 2
        finally:
            await sampler.aclose()


@pytest.mark.asyncio
async def test_cooldown_keeps_healthy_socket_and_shared_sixteen_classification_permits(tmp_path, small_pool):
    async def respond(socket, body, index):
        await socket.send(json.dumps(terminal()))
    async with endpoint(respond) as (url, headers, requests, sockets):
        async with Gateway(url) as gateway:
            ctx, _ = configured_context(tmp_path)
            gateway.allowed_opens = 1
            sampler = local_sampler(ctx, gateway.url)
            sampler.connections.connect_timeout = .04
            leases = []
            try:
                await sampler.prewarm(ctx)
                assert len(sampler.connections.idle) == 1 and sampler.connections.retry_after is not None
                leases = [await sampler.connections.lease(ctx) for _ in range(16)]
                assert leases[0].mode == "websocket" and all(row.mode == "http" for row in leases[1:])
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(sampler.connections.lease(ctx), .02)
                await leases.pop().aclose()
                replacement = await sampler.connections.lease(ctx)
                assert replacement.mode == "http" and len(gateway.opens) == 2
                await replacement.aclose()
            finally:
                await asyncio.gather(*(lease.aclose() for lease in leases))
                await sampler.aclose()
            assert sampler.connections.classifications._value == sampler.connections.sockets._value == 16


@pytest.mark.asyncio
async def test_stalled_http_headers_close_every_attempt_and_keep_turn_and_input_ids(tmp_path, small_pool):
    async def respond(socket, body, index):
        await socket.send(json.dumps(terminal()))
    async with endpoint(respond) as (url, headers, requests, sockets):
        async with Gateway(url, stall_headers=True) as gateway:
            ctx, _ = configured_context(tmp_path)
            sampler = local_sampler(ctx, gateway.url)
            sampler.connections.connect_timeout = .03
            sampler.connections.stream_idle_timeout = .04
            try:
                with pytest.raises(httpx.ReadTimeout):
                    await sampler.sample([], instructions="source policy", context=ctx)
                await finish(sampler)
                await wait_for_state(lambda: len(gateway.disconnects) == 3)
                assert len(gateway.posts) == 3
                bodies = [row["body"] for row in gateway.posts]
                assert bodies[0]["input"] == bodies[1]["input"] == bodies[2]["input"]
                assert len({row["client_metadata"]["turn_id"] for row in bodies}) == 1
                assert len({row["client_metadata"]["thread_id"] for row in bodies}) == 3
                assert sampler.connections.classifications._value == 16
                assert not gateway.errors
            finally:
                await sampler.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("published", [False, True])
async def test_actual_context_cancel_closes_http_before_headers_and_during_drain(tmp_path, small_pool, published):
    async def respond(socket, body, index):
        await socket.send(json.dumps(terminal()))
    async with endpoint(respond) as (url, headers, requests, sockets):
        events = [{"type": "response.output_text.delta", "delta": "low"}]
        async with Gateway(url, events=events, stall_headers=not published, drain=published) as gateway:
            ctx, _ = configured_context(tmp_path)
            sampler = local_sampler(ctx, gateway.url)
            sampling = asyncio.create_task(sampler.sample([], instructions="source policy", context=ctx))
            try:
                await wait_for_state(lambda: len(gateway.posts) == 1)
                if published:
                    assert await sampling == "low"
                ctx.cancel_event.set()
                if not published:
                    with pytest.raises(GuardianSamplingError, match="superseded"):
                        await sampling
                await finish(sampler)
                await wait_for_state(lambda: gateway.disconnects == [1])
                assert ctx.run_context.llm_turn_context.side_call_records[0]["status"] == "cancelled"
                assert sampler.connections.classifications._value == 16 and not sampler.connections.active
                assert not gateway.errors
            finally:
                sampling.cancel()
                await asyncio.gather(sampling, return_exceptions=True)
                await sampler.aclose()


@pytest.mark.asyncio
async def test_supersession_prefers_oldest_scored_drain_over_older_unscored_request(tmp_path, small_pool):
    async def respond(socket, body, index):
        await socket.send(json.dumps(terminal()))
    async with endpoint(respond) as (url, headers, requests, sockets):
        async with Gateway(url, stall_headers=True, drain=True,
                events=[{"type": "response.output_text.delta", "delta": "low"}]) as gateway:
            ctx, _ = configured_context(tmp_path)
            sampler = local_sampler(ctx, gateway.url)
            callers = []
            try:
                callers.append(asyncio.create_task(sampler.sample([], instructions="source policy", context=ctx)))
                await wait_for_state(lambda: len(gateway.posts) == 1)
                gateway.stall_headers = False
                assert await sampler.sample([], instructions="source policy", context=ctx) == "low"
                gateway.stall_headers = True
                for _ in range(14):
                    callers.append(asyncio.create_task(sampler.sample([], instructions="source policy", context=ctx)))
                await wait_for_state(lambda: len(gateway.posts) == 16)
                assert sampler.connections.classifications._value == 0
                callers.append(asyncio.create_task(sampler.sample([], instructions="source policy", context=ctx)))
                await wait_for_state(lambda: 2 in gateway.disconnects and len(gateway.posts) == 17)
                assert 1 not in gateway.disconnects
                assert len(sampler.active) == 16 and sampler.connections.classifications._value == 0
                assert not gateway.errors
            finally:
                for caller in callers:
                    caller.cancel()
                await asyncio.gather(*callers, return_exceptions=True)
                await sampler.aclose()
            assert sampler.connections.classifications._value == sampler.connections.sockets._value == 16


@pytest.mark.asyncio
async def test_expired_or_auth_changed_socket_is_not_reused_with_actual_provider_auth(tmp_path, small_pool):
    from backend.tests.test_history_notes_service import notes_runtime
    async def respond(socket, body, index):
        await socket.send(json.dumps({"type": "response.output_text.delta", "delta": "low"}))
        await socket.send(json.dumps(terminal()))
    async with endpoint(respond) as (url, headers, requests, sockets):
        origin = url.removesuffix("/v1")
        runtime, model, storage = notes_runtime(tmp_path, origin)
        profile = deepcopy(runtime._extension_providers[model.provider])
        profile.update({"supports_codex_backend_routes": True, "requires_openai_auth": True})
        profile["models"].append({"id": "gpt-5.6-luna", "input": ["text", "image"]})
        runtime.register_provider(model.provider, profile)
        await runtime.refresh_provider_auth(model.provider)
        ctx, _ = configured_context(tmp_path)
        config = replace(ctx.model_execution.config, model_request_policy=ModelRequestPolicy(service_tier="priority", service_tiers=("priority",)))
        ctx.model_execution = replace(ctx.model_execution, provider=model.provider, model=model.id, model_info=model, model_runtime=runtime, config=config)
        ctx.run_context.llm_turn_context = LLMTurnContext()
        sampler = LunaSampler(ctx.model_execution, conversation_id=ctx.conversation_id)
        sampler.connections.clock = lambda: 0.0
        try:
            await sampler.prewarm(ctx)
            assert len(headers) == 2 and all(row["x-codex-guardian"] == "classifier" for row in headers)
            assert await sampler.sample([], instructions="source policy", context=ctx) == "low"
            await finish(sampler)
            assert "service_tier" not in requests[0]
            old_sockets = tuple(sampler.connections.idle)
            credential = storage.get(model.provider)
            storage.set(model.provider, {**credential, "access": "actual-fixture-refresh-access"})
            # Local HTTP has no POST endpoint, so use the pool's real lease selection.
            lease = await sampler.connections.lease(ctx)
            assert lease.mode == "http" and lease.setup.headers["Authorization"] == "Bearer actual-fixture-refresh-access"
            assert all(row.socket.state == State.CLOSED for row in old_sockets)
            await lease.aclose()
            await sampler.prewarm(ctx)
            await wait_for_state(lambda: len(sampler.connections.idle) == 2)
            sampler.connections.clock = lambda: connection_pool.MAX_WEBSOCKET_AGE + .001
            lease = await sampler.connections.lease(ctx)
            assert lease.mode == "http"
            await lease.aclose()
        finally:
            await sampler.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("gate", ["disabled", "user", "full_access", "conversation", "allowed"])
async def test_service_schedule_prewarm_has_source_gates_and_host_owned_cleanup(tmp_path, gate):
    ctx, _ = configured_context(tmp_path, enabled=gate != "disabled",
        options={"async_classifier_mode": "conversation"} if gate == "conversation" else None)
    if gate == "user":
        ctx.model_execution.config.config_layer_stack.layers[-1].config["approvals_reviewer"] = "user"
    elif gate == "full_access":
        ctx.permission = replace(ctx.permission, approval_policy="never")
        ctx.sandbox_policy = replace(ctx.sandbox_policy, permission_profile=PermissionProfile.disabled())
    entered, release = asyncio.Event(), asyncio.Event()
    class HostSampler:
        closed = False
        def __init__(self, *_args, **_kwargs):
            pass
        async def prewarm(self, context):
            entered.set()
            await release.wait()
        async def aclose(self):
            self.closed = True
    owner = GuardianEvidenceOwner()
    service = GuardianAsyncService(owner.snapshot, context_source=lambda: ctx, reviews_source=lambda: (), sampler_factory=HostSampler)
    try:
        task = service.schedule_prewarm(ctx)
        if gate in {"allowed", "conversation"}:
            assert task in service.tasks and service.schedule_prewarm(ctx) is None
            await entered.wait()
            await service.aclose()
            assert task.cancelled() and not service.tasks and not service.prewarm_tasks
            assert all(row.closed for row in service.samplers.values())
        else:
            assert task is None and not service.tasks and not entered.is_set()
    finally:
        release.set()
        await service.aclose()
