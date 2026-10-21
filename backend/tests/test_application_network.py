from __future__ import annotations

import asyncio
import json
import gzip
from pathlib import Path
import ssl
from types import SimpleNamespace

import httpx
import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import ServerConnection, serve
from websockets.frames import Opcode
from websockets.http11 import Response
from websockets.datastructures import Headers
from websockets.protocol import State

from backend.application_network import (
    ApplicationHttpClient, ApplicationNetworkController, ApplicationNetworkDenied,
    ApplicationWebSocket, DestinationPolicy, application_websocket_connect,
    application_http_client,
    bind_application_network_owner, release_application_network_owner,
)
from backend.config_requirements import (
    ConfigRequirementsError, RequirementSource, RequirementsLayerEntry, compose_requirements,
)
from backend.llm.errors import classify_llm_error, llm_error_raw
from backend.llm.responses_websocket import ResponsesWebSocketPool
from backend.config import AppConfig, LLMSettings
from backend.llm.base import LLMMessage, StreamEventType
from backend.services.llm_adapter_factory import create_session_llm


def requirements(value):
    return compose_requirements([RequirementsLayerEntry(RequirementSource("enterprise_managed"), value)])


def owner(*hosts):
    controller = ApplicationNetworkController()
    controller.publish(0, DestinationPolicy(frozenset(hosts)))
    return controller, controller.policy.for_current_generation()


@pytest.mark.parametrize("domain", ["https://allowed.test", "allowed.test:443", "*.allowed.test", "a..test", "-a.test", "a-.test", "é.test", "a" * 64 + ".test"])
def test_managed_domains_reject_urls_ports_wildcards_and_non_ascii_at_ingress(domain):
    with pytest.raises(ConfigRequirementsError, match="exact ASCII"):
        requirements({"application": {"network": {"domains": {domain: "allow"}}}})


def test_requirement_precedence_normalizes_hosts_without_inventing_enabled_overrides():
    source = RequirementSource("enterprise_managed")
    merged = compose_requirements([
        RequirementsLayerEntry(source, {"application": {"network": {"enabled": False, "domains": {"ALLOWED.test.": "deny"}}}}),
        RequirementsLayerEntry(source, {"application": {"network": {"domains": {"allowed.test": "allow"}}}}),
    ])
    assert merged.application_network_policy.allows("http://anything.test")
    enabled = requirements({"application": {"network": {"domains": {"ALLOWED.test.": "allow", "denied.test": "deny"}}}})
    policy = enabled.application_network_policy
    assert policy.allows("https://allowed.test./path") and policy.allows("wss://ALLOWED.test/path")
    assert not any(policy.allows(url) for url in ("http://allowed.test", "ws://allowed.test", "https://sub.allowed.test", "https://denied.test", "http://127.0.0.1"))
    assert not requirements({"application": {"network": {}}}).application_network_policy.allows("https://allowed.test")
    with pytest.raises(ConfigRequirementsError, match="duplicate"):
        requirements({"application": {"network": {"domains": {"ALLOWED.test": "allow", "allowed.test.": "deny"}}}})


@pytest.mark.asyncio
async def test_load_failure_and_generation_replacement_revoke_original_operations_and_reject_stale_publication():
    controller, old = owner("allowed.test")
    permit = old.acquire("https://allowed.test")
    pending = asyncio.create_task(permit.run(lambda: asyncio.sleep(60)))
    await asyncio.sleep(0)
    generation = controller.invalidate()
    assert not controller.publish(generation - 1, DestinationPolicy())
    with pytest.raises(ApplicationNetworkDenied, match="revoked"):
        await asyncio.wait_for(pending, 1)
    controller.publish(generation, DestinationPolicy(frozenset({"allowed.test"})))
    with pytest.raises(ApplicationNetworkDenied, match="revoked"):
        old.acquire("https://allowed.test")
    current = controller.policy.for_current_generation()
    current_permit = current.acquire("https://allowed.test")
    def broken_load():
        raise ConfigRequirementsError("actual managed requirements parse failure")
    with pytest.raises(ConfigRequirementsError):
        controller.load_config(broken_load)
    with pytest.raises(ApplicationNetworkDenied, match="revoked"):
        current_permit.check()
    with pytest.raises(ApplicationNetworkDenied, match="unavailable"):
        current.acquire("https://allowed.test")
    config = controller.load_config(lambda: SimpleNamespace(config_layer_stack=SimpleNamespace(requirements=requirements({}))))
    config.application_network_policy.acquire("http://127.0.0.1").check()


def test_config_loading_cannot_bind_credentials_read_in_an_old_generation_to_a_new_owner(monkeypatch):
    from backend import config as config_module
    controller, _policy = owner("allowed.test")
    def load_stack(**_kwargs):
        generation = controller.invalidate()
        controller.publish(generation, DestinationPolicy())
        return SimpleNamespace(requirements=requirements({}), effective_config=lambda: {})
    monkeypatch.setattr(config_module, "load_config_layer_stack", load_stack)
    monkeypatch.setattr(config_module, "load_llm_settings", lambda _settings: LLMSettings(api_key="old-fixture", model="fixture-model"))
    bind_application_network_owner(controller)
    try:
        with pytest.raises(ApplicationNetworkDenied, match="revoked"):
            config_module.load_config()
        controller.policy.for_current_generation().acquire("https://new-owner.test").check()
    finally:
        controller.invalidate()
        release_application_network_owner(controller)


@pytest.mark.asyncio
async def test_redirect_target_is_admitted_before_its_proxy_or_transport_and_cached_response_is_revocable():
    controller, policy = owner("allowed.test", "final.test")
    resolved, sent = [], []
    async def respond(request):
        sent.append(str(request.url))
        return (httpx.Response(307, headers={"location": "https://final.test/result"})
            if request.url.host == "allowed.test" else httpx.Response(200, json={"result": "real"}))
    def route(url):
        resolved.append(url)
        return httpx.AsyncClient(transport=httpx.MockTransport(respond))
    async with ApplicationHttpClient(policy, route, follow_redirects=True) as client:
        response = await client.get("https://allowed.test/start")
        assert response.json() == {"result": "real"} and len(response.history) == 1
        assert sent == ["https://allowed.test/start", "https://final.test/result"]
        controller.publish(0, DestinationPolicy(frozenset({"allowed.test"})))
        for read in (lambda: response.content, lambda: response.text, response.json):
            with pytest.raises(ApplicationNetworkDenied, match="revoked"):
                read()
        with pytest.raises(ApplicationNetworkDenied, match="destination"):
            await client.get("https://allowed.test/start")
        assert sent == ["https://allowed.test/start", "https://final.test/result", "https://allowed.test/start"]
        assert resolved == ["https://allowed.test/start", "https://final.test/result"]


@pytest.mark.asyncio
async def test_response_header_wait_is_cancelled_when_original_permit_is_revoked():
    controller, policy = owner("allowed.test")
    entered, cancelled = asyncio.Event(), asyncio.Event()
    async def respond(_request):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    async with ApplicationHttpClient(policy, lambda _url: httpx.AsyncClient(transport=httpx.MockTransport(respond))) as client:
        task = asyncio.create_task(client.get("https://allowed.test"))
        await entered.wait()
        controller.publish(0, DestinationPolicy(frozenset()))
        with pytest.raises(ApplicationNetworkDenied, match="revoked"):
            await asyncio.wait_for(task, 1)
        assert cancelled.is_set()


@pytest.mark.asyncio
async def test_response_body_wait_closes_the_actual_stream_on_revocation():
    controller, policy = owner("allowed.test")
    entered, closed = asyncio.Event(), asyncio.Event()
    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"first"
            entered.set()
            await asyncio.Event().wait()
        async def aclose(self):
            closed.set()
    async def respond(_request):
        return httpx.Response(200, stream=Body())
    async with ApplicationHttpClient(policy, lambda _url: httpx.AsyncClient(transport=httpx.MockTransport(respond))) as client:
        response = await client.send(client.build_request("GET", "https://allowed.test"), stream=True)
        chunks = response.aiter_bytes()
        assert await anext(chunks) == b"first"
        task = asyncio.create_task(anext(chunks))
        await entered.wait()
        controller.publish(0, DestinationPolicy(frozenset()))
        with pytest.raises(ApplicationNetworkDenied, match="revoked"):
            await asyncio.wait_for(task, 1)
        assert closed.is_set()


@pytest.mark.asyncio
async def test_managed_unrestricted_real_http_keeps_normal_gzip_sse_streaming_and_loopback_behavior():
    controller = ApplicationNetworkController()
    controller.publish(0, DestinationPolicy())
    data = b'data: {"type":"delta","text":"fixture"}\n\ndata: [DONE]\n\n'
    body = gzip.compress(data)
    async def respond(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Encoding: gzip\r\nContent-Length: "
            + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
        await writer.drain()
        writer.close()
        await writer.wait_closed()
    async with await asyncio.start_server(respond, "127.0.0.1", 0) as server:
        url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/responses"
        async with application_http_client(url, policy=controller.policy.for_current_generation(), proxy_mode="direct") as client:
            async with client.stream("GET", url) as response:
                assert [line async for line in response.aiter_lines()] == data.decode().splitlines()


class Socket:
    def __init__(self, *, block=False):
        self.state = State.OPEN
        self.response = SimpleNamespace(status_code=200, headers={})
        self.closed = asyncio.Event()
        self.reading, self.writing = asyncio.Event(), asyncio.Event()
        self.block = block
    async def close(self, **_kwargs):
        self.state = State.CLOSED
        self.closed.set()
    async def handshake(self, *_args, **_kwargs):
        return None
    def start_keepalive(self):
        return None
    async def send(self, _message, **_kwargs):
        self.writing.set()
        if self.block:
            await asyncio.Event().wait()
    async def recv(self, **_kwargs):
        self.reading.set()
        if self.block:
            await asyncio.Event().wait()
        return json.dumps({"type": "response.completed", "response": {"id": "actual-response", "output": []}})


@pytest.mark.asyncio
async def test_pending_websocket_read_and_write_both_keep_the_original_policy_reason():
    controller, policy = owner("allowed.test")
    raw = Socket(block=True)
    socket = ApplicationWebSocket(raw, policy.acquire("wss://allowed.test"))
    tasks = [asyncio.create_task(socket.recv()), asyncio.create_task(socket.send("message"))]
    await raw.reading.wait()
    await raw.writing.wait()
    controller.invalidate()
    for task in tasks:
        with pytest.raises(ApplicationNetworkDenied, match="revoked"):
            await asyncio.wait_for(task, 1)
    assert raw.closed.is_set()
    await socket.close()


@pytest.mark.asyncio
async def test_real_idle_websocket_is_closed_so_its_autonomous_ping_task_cannot_outlive_revocation():
    pinged = asyncio.Event()
    pings = []
    class Peer(ServerConnection):
        def process_event(self, event):
            if getattr(event, "opcode", None) == Opcode.PING:
                pings.append(event)
                pinged.set()
            super().process_event(event)
    async def receive(socket):
        await socket.wait_closed()
    controller = ApplicationNetworkController()
    controller.publish(0, DestinationPolicy())
    async with serve(receive, "127.0.0.1", 0, create_connection=Peer) as server:
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        socket = await application_websocket_connect(url, connect, policy=controller.policy.for_current_generation(),
            proxy_resolver=lambda _url: None, ping_interval=0.01, ping_timeout=1)
        try:
            await asyncio.wait_for(pinged.wait(), 1)
            controller.publish(0, DestinationPolicy(frozenset()))
            await asyncio.wait_for(socket.socket.wait_closed(), 1)
            count = len(pings)
            await asyncio.sleep(0.03)
            assert socket.socket.state == State.CLOSED and len(pings) == count
            with pytest.raises(ApplicationNetworkDenied, match="revoked"):
                await socket.send("after revocation")
        finally:
            await socket.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("allow_redirect", [False, True])
async def test_real_websocket_redirect_is_admitted_before_target_proxy_dns_and_retains_the_final_target_permit(allow_redirect):
    tls = Path(__file__).parent / "fixtures/application_network_tls"
    server_ssl = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ssl.load_cert_chain(tls / "cert.pem", tls / "key.pem")
    client_ssl = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    client_ssl.check_hostname = False
    client_ssl.verify_mode = ssl.CERT_NONE
    controller, policy = owner("localhost", *(["127.0.0.1"] if allow_redirect else []))
    reached, resolved = [], []
    async def target(socket):
        reached.append(socket)
        await socket.wait_closed()
    async with serve(target, "127.0.0.1", 0, ssl=server_ssl) as destination:
        destination_url = f"wss://127.0.0.1:{destination.sockets[0].getsockname()[1]}/final"
        def redirect(_socket, _request):
            return Response(302, "Found", Headers({"location": destination_url, "content-length": "0"}), b"")
        async with serve(target, "127.0.0.1", 0, ssl=server_ssl, process_request=redirect) as origin:
            url = f"wss://localhost:{origin.sockets[0].getsockname()[1]}/start"
            def resolve(destination):
                resolved.append(destination)
                return None
            if not allow_redirect:
                with pytest.raises(ApplicationNetworkDenied, match="destination"):
                    await application_websocket_connect(url, connect, policy=policy, proxy_resolver=resolve, ssl=client_ssl)
                assert not reached and destination_url not in resolved
            else:
                socket = await application_websocket_connect(url, connect, policy=policy, proxy_resolver=resolve, ssl=client_ssl)
                try:
                    assert len(reached) == 1 and destination_url in resolved
                    controller.publish(0, DestinationPolicy(frozenset({"localhost"})))
                    await asyncio.wait_for(socket.socket.wait_closed(), 1)
                    assert socket.socket.state == State.CLOSED
                finally:
                    await socket.close()


@pytest.mark.asyncio
async def test_responses_pool_does_not_reuse_the_revoked_original_idle_socket(monkeypatch):
    controller, policy = owner("allowed.test")
    sockets = []
    class Dial(connect):
        async def create_connection(self):
            socket = Socket()
            sockets.append(socket)
            return socket
    monkeypatch.setattr("backend.llm.responses_websocket.connect", Dial)
    pool = ResponsesWebSocketPool(policy=policy)
    async def handshake(_status, _headers):
        return None
    async def turn():
        request = pool.request(("original-owner",))
        async for _event in request.events({"input": []}, url="wss://allowed.test", headers={}, proxy=None,
                wire_payload_sink=None, on_handshake=handshake):
            pass
        request.accepted = True
        await request.aclose()
        return request
    try:
        await turn()
        assert pool.idle is not None
        controller.publish(0, DestinationPolicy(frozenset()))
        await asyncio.wait_for(sockets[0].closed.wait(), 1)
        controller.publish(0, DestinationPolicy(frozenset({"allowed.test"})))
        second = await turn()
        assert len(sockets) == 2 and not second.info["connection_reused"]
    finally:
        await pool.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("restricted", [False, True])
async def test_managed_unknown_websocket_sdk_is_rejected_before_invocation_or_proxy_resolution(restricted):
    controller, policy = owner("allowed.test")
    if not restricted:
        controller.publish(0, DestinationPolicy())
    calls = []
    def unknown(*_args, **_kwargs):
        calls.append("connector")
        raise AssertionError("Unknown SDK was invoked")
    def resolve(_url):
        calls.append("proxy")
        raise AssertionError("Unknown SDK resolved a proxy")
    with pytest.raises(ApplicationNetworkDenied, match="SDK transport"):
        await application_websocket_connect("wss://allowed.test", unknown, policy=policy, proxy_resolver=resolve)
    assert not calls


def test_policy_error_is_not_retryable_network_or_fallback_and_retains_its_actual_reason():
    error = ApplicationNetworkDenied("revoked")
    classification = classify_llm_error(error)
    assert classification.fatal and not classification.retryable and classification.provider_error_type == "application_network"
    raw = llm_error_raw(error, "fixture")
    assert raw["provider_error_code"] == "application_network_revoked" and "revoked" in raw["provider_error_message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("wire", ["chat", "responses", "responses-websocket", "anthropic"])
@pytest.mark.parametrize("stale", [False, True])
async def test_production_adapters_admit_before_proxy_and_do_not_turn_policy_failure_into_retry_or_fallback(monkeypatch, wire, stale):
    from backend.agent.provider_stream_error_event import provider_error_details
    controller, policy = owner("other.test")
    config = AppConfig(llm=LLMSettings(api_key="fixture", model="fixture-model", base_url="https://denied.test/v1",
        wire_api="responses" if wire == "responses-websocket" else wire, responses_websocket=wire == "responses-websocket"),
        application_network_policy=policy)
    if stale:
        generation = controller.invalidate()
        controller.publish(generation, DestinationPolicy())
    proxy_calls = []
    def forbidden(*args, **kwargs):
        proxy_calls.append((args, kwargs))
        raise AssertionError("Policy failure reached proxy resolution")
    monkeypatch.setattr("backend.llm.proxy_policy.provider_httpx_proxy_kwargs", forbidden)
    monkeypatch.setattr("backend.llm.openai_adapter._proxy_url_for_base_url", forbidden)
    adapter = create_session_llm(config)
    try:
        events = [event async for event in adapter.stream_chat([LLMMessage(role="user", content="ping")], metadata={"thread_id": "thread", "turn_id": "turn"})]
        errors = [event for event in events if event.type == StreamEventType.ERROR]
        assert len(errors) == 1 and not proxy_calls
        classification = provider_error_details(errors[0])[1]
        assert classification.fatal and not classification.retryable and classification.provider_error_type == "application_network"
        assert errors[0].raw["provider_error_code"] == "application_network_" + ("revoked" if stale else "destination")
        if wire == "responses-websocket":
            assert not adapter._responses_websocket.fallback_to_http
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("wire", ["chat", "responses", "anthropic"])
async def test_settings_discovery_and_connection_checks_keep_policy_denial_as_a_non_retryable_permission_result(monkeypatch, wire):
    from backend.api.models import LLMSettingsUpdateRequest
    from backend.services import llm_provider_service
    controller, _policy = owner("other.test")
    current = {"proxy_mode": "direct", "default_headers": {}, "auth_header": False, "base_url": "https://denied.test/v1",
        "api_key": "fixture", "model": "fixture-model", "wire_api": wire, "image_mode": "disabled", "image_model": "",
        "available_models": ["fixture-model"], "model_metadata": {}}
    monkeypatch.setattr(llm_provider_service, "get_custom_settings", lambda: current)
    request = LLMSettingsUpdateRequest.model_validate({"provider": "custom", "custom": {
        "base_url": current["base_url"], "api_key": "fixture", "model": "fixture-model", "wire_api": wire,
        "image_mode": "disabled", "available_models": ["fixture-model"]}})
    bind_application_network_owner(controller)
    try:
        discovered = await llm_provider_service.refresh_llm_models(request)
        checked = await llm_provider_service.check_llm_connection(request)
        assert discovered["failure_kind"] == "application_network_policy" and not discovered["retryable"]
        assert not checked["ok"] and checked["failure_kind"] == "application_network_policy" and not checked["retryable"]
        assert checked["model_discovery_failure_kind"] == "application_network_policy" and not checked["model_discovery_retryable"]
        assert "destination denied" in checked["message"]
    finally:
        controller.invalidate()
        release_application_network_owner(controller)
