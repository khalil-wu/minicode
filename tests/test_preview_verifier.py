import pytest

from backend.preview import verifier
from backend.preview.verifier import PreviewVerification, verify_preview_url, wait_until_ready


@pytest.mark.anyio
async def test_preview_verifier_rejects_non_http_url():
    result = await verify_preview_url("file:///tmp/index.html")

    assert result.ok is False
    assert result.status_code is None
    assert "http" in result.error


@pytest.mark.anyio
async def test_preview_verifier_reports_connection_error():
    result = await verify_preview_url("http://127.0.0.1:9", timeout=0.2)

    assert result.ok is False
    assert result.status_code is None
    assert result.elapsed_ms >= 0
    assert result.error


@pytest.mark.anyio
async def test_wait_until_ready_polls_until_success(monkeypatch):
    calls = 0

    async def fake_verify(url: str, timeout: float = 8.0):
        nonlocal calls
        calls += 1
        return PreviewVerification(
            url=url,
            ok=calls >= 2,
            status_code=200 if calls >= 2 else None,
            elapsed_ms=5,
            error="" if calls >= 2 else "connection refused",
        )

    monkeypatch.setattr(verifier, "verify_preview_url", fake_verify)

    result = await wait_until_ready("http://127.0.0.1:5173", timeout=1.0, interval=0.01)

    assert result.ok is True
    assert result.status_code == 200
    assert calls == 2


@pytest.mark.anyio
async def test_preview_verifier_rejects_private_peer_after_public_dns_check(monkeypatch):
    class _Stream:
        def get_extra_info(self, name):
            return ("169.254.169.254", 80) if name == "server_addr" else None

    class _Response:
        status_code = 200
        url = "https://preview.example/app"
        headers = {}
        extensions = {"network_stream": _Stream()}

    class _Client:
        max_redirects = 10

        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def get(self, _url):
            return _Response()

    monkeypatch.setattr(verifier, "assess_network_url", lambda _url: type("A", (), {"allowed": True, "reason": ""})())
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    result = await verify_preview_url("https://preview.example/app")

    assert result.ok is False
    assert "private peer" in result.error


@pytest.mark.anyio
async def test_preview_verifier_accepts_public_peer(monkeypatch):
    class _Stream:
        def get_extra_info(self, name):
            return ("93.184.216.34", 443) if name == "server_addr" else None

    class _Response:
        status_code = 200
        url = "https://preview.example/app"
        headers = {}
        extensions = {"network_stream": _Stream()}

    class _Client:
        max_redirects = 10

        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def get(self, _url):
            return _Response()

    monkeypatch.setattr(verifier, "assess_network_url", lambda _url: type("A", (), {"allowed": True, "reason": ""})())
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    result = await verify_preview_url("https://preview.example/app")

    assert result.ok is True
    assert result.status_code == 200


@pytest.mark.anyio
async def test_preview_verifier_fails_closed_when_public_peer_is_hidden(monkeypatch):
    class _Response:
        status_code = 200
        url = "https://preview.example/app"
        headers = {}
        extensions = {}

    class _Client:
        max_redirects = 10

        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def get(self, _url):
            return _Response()

    monkeypatch.setattr(
        verifier,
        "assess_network_url",
        lambda _url: type("A", (), {"allowed": True, "reason": ""})(),
    )
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    result = await verify_preview_url("https://preview.example/app")

    assert result.ok is False
    assert "peer could not be verified" in result.error


@pytest.mark.anyio
async def test_preview_verifier_rejects_invalid_localhost_port(monkeypatch):
    result = await verify_preview_url("http://localhost:invalid/")

    assert result.ok is False
    assert result.error == "Preview URL port is invalid"
