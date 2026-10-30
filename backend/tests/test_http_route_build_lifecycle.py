"""Cold HTTP route preparation keeps the loop responsive and owns its result."""
from __future__ import annotations

import asyncio
import ssl
import threading

import httpx
import pytest

import backend.application_network as network


def policy():
    owner = network.ApplicationNetworkController()
    assert owner.publish(0, network.DestinationPolicy())
    return owner


def route_client(sent):
    async def respond(request):
        sent.append(str(request.url))
        return httpx.Response(200, text="ready")
    return httpx.AsyncClient(transport=httpx.MockTransport(respond))


@pytest.mark.asyncio
async def test_cold_route_keeps_loop_live_and_parallel_requests_build_once():
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()
    calls, clients, sent = [], [], []

    def build(destination):
        calls.append(destination)
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(2), "route preparation must not block the releasing loop"
        client = route_client(sent)
        clients.append(client)
        return client

    async with network.ApplicationHttpClient(policy().policy, build) as client:
        first = asyncio.create_task(client.get("https://route.test/one"))
        second = asyncio.create_task(client.get("https://route.test/two"))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            assert len(calls) == 1 and not first.done() and not second.done()
        finally:
            release.set()
        assert [response.text for response in await asyncio.gather(first, second)] == ["ready", "ready"]
        assert len(calls) == 1 and len(sent) == 2
    assert clients[0].is_closed


@pytest.mark.asyncio
async def test_cancelled_build_remains_owned_until_close_can_release_it():
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()
    clients, sent = [], []

    def build(_destination):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(2)
        client = route_client(sent)
        clients.append(client)
        return client

    client = network.ApplicationHttpClient(policy().policy, build)
    sending = asyncio.create_task(client.get("https://route.test"))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        sending.cancel()
        await asyncio.sleep(0)
        closing = asyncio.create_task(client.aclose())
        await asyncio.sleep(0)
        assert not sending.done() and not closing.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await sending
    await closing
    assert len(clients) == 1 and clients[0].is_closed and not sent
    with pytest.raises(RuntimeError, match="client has been closed"):
        await client.get("https://other.test")


@pytest.mark.asyncio
async def test_revocation_during_build_prevents_the_actual_send():
    owner = policy()
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()
    clients, sent = [], []

    def build(_destination):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(2)
        client = route_client(sent)
        clients.append(client)
        return client

    async with network.ApplicationHttpClient(owner.policy, build) as client:
        sending = asyncio.create_task(client.get("https://route.test"))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            owner.publish(0, network.DestinationPolicy(frozenset()))
        finally:
            release.set()
        with pytest.raises(network.ApplicationNetworkDenied, match="revoked"):
            await sending
        assert not sent
    assert clients[0].is_closed


@pytest.mark.asyncio
async def test_revoked_queued_route_does_not_read_proxy_or_construct_a_client():
    owner = policy()
    loop = asyncio.get_running_loop()
    entered, queued = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    built, clients, sent = [], [], []
    acquire = owner.policy.acquire

    def admitted(destination):
        permit = acquire(destination)
        if "queued.test" in destination:
            queued.set()
        return permit

    owner.policy.acquire = admitted

    def build(destination):
        built.append(destination)
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(2)
        client = route_client(sent)
        clients.append(client)
        return client

    async with network.ApplicationHttpClient(owner.policy, build) as client:
        first = asyncio.create_task(client.get("https://allowed.test"))
        second = None
        try:
            await asyncio.wait_for(entered.wait(), 1)
            second = asyncio.create_task(client.get("https://queued.test"))
            await asyncio.wait_for(queued.wait(), 1)
            owner.publish(0, network.DestinationPolicy(frozenset({"allowed.test"})))
        finally:
            release.set()
        assert (await first).text == "ready"
        with pytest.raises(network.ApplicationNetworkDenied, match="revoked"):
            await second
        assert built == ["https://allowed.test"] and sent == ["https://allowed.test"]
    assert clients[0].is_closed


@pytest.mark.asyncio
async def test_warm_route_does_not_wait_for_another_cold_route():
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()
    clients, calls, sent = [], [], []

    def build(destination):
        calls.append(destination)
        if "cold.test" in destination:
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(2)
        client = route_client(sent)
        clients.append(client)
        return client

    async with network.ApplicationHttpClient(policy().policy, build) as client:
        assert (await client.get("https://warm.test/first")).text == "ready"
        cold = asyncio.create_task(client.get("https://cold.test/first"))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            assert (await asyncio.wait_for(client.get("https://warm.test/next"), 1)).text == "ready"
            assert not cold.done()
        finally:
            release.set()
        assert (await cold).text == "ready"
    assert len(calls) == 2 and all(item.is_closed for item in clients)


@pytest.mark.asyncio
async def test_route_build_error_is_not_cached_or_hidden():
    calls, sent = [], []

    def build(destination):
        calls.append(destination)
        if len(calls) == 1:
            raise ValueError("route configuration is unreadable")
        return route_client(sent)

    async with network.ApplicationHttpClient(policy().policy, build) as client:
        with pytest.raises(ValueError, match="configuration is unreadable"):
            await client.get("https://route.test/first")
        assert not client.routes and not sent
        assert (await client.get("https://route.test/second")).text == "ready"
        assert len(calls) == 2 and len(sent) == 1


@pytest.mark.asyncio
async def test_parallel_clients_share_one_default_ca_load(monkeypatch):
    original_client = httpx.AsyncClient
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()
    loads, routes = [], []

    def load(**_options):
        loads.append(1)
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(2)
        return ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

    def build(**options):
        options["transport"] = httpx.MockTransport(lambda _request: httpx.Response(200, text="ready"))
        client = original_client(**options)
        routes.append(client)
        return client

    monkeypatch.setattr(network.ssl, "create_default_context", load)
    monkeypatch.setattr(network.httpx, "AsyncClient", build)
    network._load_http_ssl_context.cache_clear()
    owner = policy()
    try:
        async with network.application_http_client("https://one.test", policy=owner.policy, proxy_mode="direct") as one, \
                network.application_http_client("https://two.test", policy=owner.policy, proxy_mode="direct") as two:
            tasks = [asyncio.create_task(one.get("https://one.test")), asyncio.create_task(two.get("https://two.test"))]
            try:
                await asyncio.wait_for(entered.wait(), 1)
                assert len(loads) == 1
            finally:
                release.set()
            assert [r.text for r in await asyncio.gather(*tasks)] == ["ready", "ready"]
        assert len(loads) == 1 and len(routes) == 2 and all(r.is_closed for r in routes)
    finally:
        network._load_http_ssl_context.cache_clear()


@pytest.mark.asyncio
async def test_closed_borrowing_wrapper_does_not_reuse_a_live_external_client():
    sent = []
    borrowed = route_client(sent)
    client = network.ApplicationHttpClient(policy().policy, lambda _destination: borrowed, owns_clients=False)
    try:
        assert (await client.get("https://route.test")).text == "ready"
        await client.aclose()
        with pytest.raises(RuntimeError, match="client has been closed"):
            await client.get("https://route.test")
        with pytest.raises(RuntimeError, match="client has been closed"):
            await client.get("https://other.test")
        assert len(sent) == 1 and not borrowed.is_closed
    finally:
        await client.aclose()
        await borrowed.aclose()
