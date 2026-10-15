from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest
from fastapi import Response

from backend.api import routes_health


@pytest.mark.parametrize("endpoint,canonical", [
    ("http://LOCALHOST:9347/", "http://localhost:9347"),
    ("http://127.0.0.1:9347", "http://127.0.0.1:9347"),
    ("http://[::1]:9347/", "http://[::1]:9347"),
    ("http://127.0.0.1:80/", "http://127.0.0.1"),
])
@pytest.mark.asyncio
async def test_readiness_attests_actual_private_host_environment_without_secret_values(monkeypatch, endpoint, canonical):
    secret = "private-host-token"
    monkeypatch.setenv("MINICODE_EMBEDDED_BROWSER_ENDPOINT", endpoint)
    monkeypatch.setenv("MINICODE_EMBEDDED_BROWSER_TOKEN", secret)
    monkeypatch.setattr(routes_health._state, "bootstrap", SimpleNamespace(config=object()))
    response = Response()
    readiness = await routes_health.readiness_check(response)
    assert readiness["desktop_bridge_fingerprint"] == hashlib.sha256(f"{canonical}\0{secret}".encode()).hexdigest()
    assert secret not in repr(readiness) and endpoint not in repr(readiness)
    assert readiness["ready"] and response.status_code == 200


def test_headless_runtime_does_not_claim_a_desktop_host_binding(monkeypatch):
    monkeypatch.delenv("MINICODE_EMBEDDED_BROWSER_ENDPOINT", raising=False)
    monkeypatch.delenv("MINICODE_EMBEDDED_BROWSER_TOKEN", raising=False)
    assert routes_health._desktop_bridge_fingerprint() == ""


def test_attestation_hashes_the_actual_private_token_used_by_backend_transports(monkeypatch):
    endpoint = "http://127.0.0.1:9347"
    token = " private-host-token "
    monkeypatch.setenv("MINICODE_EMBEDDED_BROWSER_ENDPOINT", endpoint)
    monkeypatch.setenv("MINICODE_EMBEDDED_BROWSER_TOKEN", token)
    assert routes_health._desktop_bridge_fingerprint() == hashlib.sha256(f"{endpoint}\0{token}".encode()).hexdigest()
