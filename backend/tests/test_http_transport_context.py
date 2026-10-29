from __future__ import annotations

import asyncio
from pathlib import Path
import shutil
import ssl

import httpx
import pytest

from backend.application_network import (
    ApplicationHttpClient,
    ApplicationNetworkController,
    ApplicationNetworkDenied,
    ApplicationNetworkPolicy,
    DestinationPolicy,
    application_http_client,
    application_http_ssl_context,
)


TLS_FIXTURE = Path(__file__).parent / "fixtures/application_network_tls"


def test_default_ca_bundle_reuses_verification_material_and_reloads_changed_bundle(tmp_path, monkeypatch):
    bundle = tmp_path / "roots.pem"
    shutil.copyfile(TLS_FIXTURE / "cert.pem", bundle)
    monkeypatch.setattr("certifi.where", lambda: str(bundle))

    original = application_http_ssl_context()
    assert original.verify_mode == ssl.CERT_REQUIRED and original.check_hostname
    assert original.cert_store_stats()["x509"] == 1
    assert application_http_ssl_context() is original

    with bundle.open("a", encoding="utf-8") as handle:
        handle.write("\n")
    updated = application_http_ssl_context()
    assert updated is not original
    assert updated.cert_store_stats() == original.cert_store_stats()
    assert updated.verify_mode == ssl.CERT_REQUIRED and updated.check_hostname


@pytest.mark.asyncio
async def test_managed_denied_request_does_not_build_an_unused_tls_transport(monkeypatch):
    controller = ApplicationNetworkController()
    controller.publish(0, DestinationPolicy(frozenset()))

    def forbidden(*_args, **_kwargs):
        raise AssertionError("TLS context was built before network admission")

    monkeypatch.setattr(ssl, "create_default_context", forbidden)
    async with application_http_client("https://denied.test", policy=controller.policy) as client:
        with pytest.raises(ApplicationNetworkDenied, match="destination"):
            await client.get("https://denied.test")


@pytest.mark.asyncio
@pytest.mark.parametrize("managed", [False, True])
async def test_custom_transport_does_not_require_unrelated_default_ca_material(managed, monkeypatch):
    def forbidden():
        raise AssertionError("The custom transport does not use the default TLS context")

    monkeypatch.setattr("backend.application_network.application_http_ssl_context", forbidden)
    controller = ApplicationNetworkController()
    controller.publish(0, DestinationPolicy())
    policy = controller.policy if managed else ApplicationNetworkPolicy()
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, text="custom transport"))
    async with application_http_client("https://custom.test", policy=policy, proxy_mode="direct", transport=transport) as client:
        assert (await client.get("https://custom.test")).text == "custom transport"


@pytest.mark.asyncio
@pytest.mark.parametrize("owns_clients", [False, True])
async def test_context_exit_closes_owned_routes_once_and_retains_borrowed_client(owns_clients):
    class Transport(httpx.AsyncBaseTransport):
        def __init__(self):
            self.closed = 0

        async def handle_async_request(self, _request):
            return httpx.Response(200, text="route response")

        async def aclose(self):
            self.closed += 1

    transport = Transport()
    route_client = httpx.AsyncClient(transport=transport)
    controller = ApplicationNetworkController()
    controller.publish(0, DestinationPolicy())
    try:
        async with ApplicationHttpClient(controller.policy, lambda _url: route_client, owns_clients=owns_clients) as client:
            assert (await client.get("https://first.test")).text == "route response"
            assert (await client.get("https://second.test")).text == "route response"
        assert client.is_closed
        assert route_client.is_closed is owns_clients
        assert transport.closed == int(owns_clients)
    finally:
        await route_client.aclose()
    assert transport.closed == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("managed", [False, True])
async def test_client_identity_does_not_leak_into_shared_default_context(managed, monkeypatch):
    monkeypatch.setattr("certifi.where", lambda: str(TLS_FIXTURE / "cert.pem"))
    server_ssl = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ssl.load_cert_chain(TLS_FIXTURE / "cert.pem", TLS_FIXTURE / "key.pem")
    server_ssl.load_verify_locations(TLS_FIXTURE / "cert.pem")
    server_ssl.verify_mode = ssl.CERT_OPTIONAL
    identities = []

    async def respond(reader, writer):
        identities.append(bool(writer.get_extra_info("peercert")))
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    controller = ApplicationNetworkController()
    controller.publish(0, DestinationPolicy())
    policy = controller.policy if managed else ApplicationNetworkPolicy()
    async with await asyncio.start_server(respond, "127.0.0.1", 0, ssl=server_ssl) as server:
        url = f"https://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        async with application_http_client(url, policy=policy, proxy_mode="direct",
                cert=(str(TLS_FIXTURE / "cert.pem"), str(TLS_FIXTURE / "key.pem"))) as client:
            assert (await client.get(url)).text == "ok"
        async with application_http_client(url, policy=policy, proxy_mode="direct") as client:
            assert (await client.get(url)).text == "ok"
    assert identities == [True, False]


@pytest.mark.asyncio
async def test_explicit_ca_context_still_controls_verification_without_changing_default_trust():
    server_ssl = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ssl.load_cert_chain(TLS_FIXTURE / "cert.pem", TLS_FIXTURE / "key.pem")

    async def respond(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async with await asyncio.start_server(respond, "127.0.0.1", 0, ssl=server_ssl) as server:
        url = f"https://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        custom = ssl.create_default_context(cafile=str(TLS_FIXTURE / "cert.pem"))
        async with application_http_client(url, policy=ApplicationNetworkPolicy(), proxy_mode="direct", verify=custom) as client:
            assert (await client.get(url)).text == "ok"
        async with application_http_client(url, policy=ApplicationNetworkPolicy(), proxy_mode="direct") as client:
            with pytest.raises(httpx.ConnectError, match="CERTIFICATE_VERIFY_FAILED"):
                await client.get(url)
