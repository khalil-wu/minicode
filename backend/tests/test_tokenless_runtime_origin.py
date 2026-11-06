"""Tokenless (dev) runtime auth must not accept cross-site browser requests.

Simple requests (text/plain POST, no preflight) reach the handler even though
CORS hides the response, so the Origin header has to be checked server-side.
"""
from __future__ import annotations

import signal

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from backend import main
from backend.api.auth import RUNTIME_TOKEN_ENV, _is_runtime_authorized


def _request(origin: str | None, *, client_host: str = "127.0.0.1", port: int = 8000) -> Request:
    headers = [(b"host", f"127.0.0.1:{port}".encode())]
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    return Request({
        "type": "http", "method": "POST", "scheme": "http", "path": "/api/runtime/shutdown",
        "raw_path": b"/api/runtime/shutdown", "query_string": b"", "headers": headers,
        "client": (client_host, 50000), "server": ("127.0.0.1", port),
    })


@pytest.fixture
def tokenless(monkeypatch):
    monkeypatch.delenv(RUNTIME_TOKEN_ENV, raising=False)
    monkeypatch.delenv("MINICODE_CORS_ORIGINS", raising=False)
    monkeypatch.delenv("MINICODE_FRONTEND_URL", raising=False)
    monkeypatch.delenv("VITE_DEV_FRONTEND_ORIGIN", raising=False)
    fired: list[object] = []
    monkeypatch.setattr(signal, "raise_signal", lambda sig: fired.append(sig))
    return fired


@pytest.mark.parametrize("origin", [
    "https://evil.example", "http://evil.example:5173", "null", "",
    "http://mcp-app-abc.localhost:8000",
])
def test_tokenless_rejects_cross_site_and_opaque_origins(tokenless, origin) -> None:
    assert not _is_runtime_authorized(_request(origin))


@pytest.mark.parametrize("origin", [
    None,  # CLI / curl / non-browser clients send no Origin
    "http://127.0.0.1:8000",  # UI served by the backend itself
    "http://localhost:5173",  # vite dev server
    "http://127.0.0.1:5199",  # dev regex range
])
def test_tokenless_keeps_loopback_and_non_browser_callers(tokenless, origin) -> None:
    assert _is_runtime_authorized(_request(origin))


def test_tokenless_still_rejects_non_loopback_clients(tokenless) -> None:
    assert not _is_runtime_authorized(_request(None, client_host="192.168.1.20"))


def test_configured_frontend_origin_is_allowed(tokenless, monkeypatch) -> None:
    monkeypatch.setenv("MINICODE_FRONTEND_URL", "http://127.0.0.1:43173/workbench/")
    assert _is_runtime_authorized(_request("http://127.0.0.1:43173"))


def test_token_mode_is_unchanged_by_origin(monkeypatch) -> None:
    monkeypatch.setenv(RUNTIME_TOKEN_ENV, "secret-token")
    request = _request("null")
    request.scope["headers"].append((b"x-minicode-token", b"secret-token"))
    assert _is_runtime_authorized(request)
    assert not _is_runtime_authorized(_request("http://127.0.0.1:8000"))


@pytest.mark.parametrize("path", ["/api/runtime/shutdown", "/api/plugins/runtime/refresh"])
def test_cross_site_simple_post_cannot_reach_bodyless_mutations(tokenless, path) -> None:
    client = TestClient(main.app)  # no lifespan: middleware decides before any handler
    response = client.post(path, headers={"Origin": "https://evil.example", "Content-Type": "text/plain"},
                           content=b"{}")
    assert response.status_code == 401
    assert tokenless == []
