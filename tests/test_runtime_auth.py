import pytest
import importlib
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backend.main import app


def test_runtime_auth_disabled_by_default(monkeypatch) -> None:
    monkeypatch.delenv("MINICODE_RUNTIME_TOKEN", raising=False)

    with TestClient(app) as client:
        response = client.get("/api/status")

    assert response.status_code == 200


def test_runtime_auth_rejects_api_without_token(monkeypatch) -> None:
    monkeypatch.setenv("MINICODE_RUNTIME_TOKEN", "secret-token")

    with TestClient(app) as client:
        response = client.get("/api/status")

    assert response.status_code == 401


def test_runtime_auth_allows_api_header_token(monkeypatch) -> None:
    monkeypatch.setenv("MINICODE_RUNTIME_TOKEN", "secret-token")

    with TestClient(app) as client:
        response = client.get("/api/status", headers={"X-MiniCode-Token": "secret-token"})

    assert response.status_code == 200


def test_runtime_auth_rejects_api_query_token(monkeypatch) -> None:
    monkeypatch.setenv("MINICODE_RUNTIME_TOKEN", "secret-token")

    with TestClient(app) as client:
        response = client.get("/api/status?minicode_token=secret-token")

    assert response.status_code == 401


def test_runtime_auth_allows_cors_preflight_without_token(monkeypatch) -> None:
    monkeypatch.setenv("MINICODE_RUNTIME_TOKEN", "secret-token")

    with TestClient(app) as client:
        response = client.options(
            "/api/status",
            headers={
                "Origin": "http://127.0.0.1:5173",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "X-MiniCode-Token",
            },
        )

    assert response.status_code != 401


def test_runtime_auth_allows_dynamic_vite_dev_port_preflight(monkeypatch) -> None:
    import backend.main as backend_main

    monkeypatch.setenv("MINICODE_RUNTIME_TOKEN", "secret-token")
    monkeypatch.setenv("MINICODE_FRONTEND_URL", "http://127.0.0.1:5175")
    reloaded_main = importlib.reload(backend_main)

    try:
        with TestClient(reloaded_main.app) as client:
            response = client.options(
                "/api/llm/models/refresh",
                headers={
                    "Origin": "http://127.0.0.1:5175",
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "content-type,X-MiniCode-Token",
                },
            )
    finally:
        importlib.reload(backend_main)

    assert response.status_code == 200


def test_runtime_auth_allows_electron_file_origin_preflight_with_runtime_token(monkeypatch) -> None:
    import backend.main as backend_main

    monkeypatch.setenv("MINICODE_RUNTIME_TOKEN", "secret-token")
    reloaded_main = importlib.reload(backend_main)

    try:
        with TestClient(reloaded_main.app) as client:
            response = client.options(
                "/api/status",
                headers={
                    "Origin": "null",
                    "Access-Control-Request-Method": "GET",
                    "Access-Control-Request-Headers": "X-MiniCode-Token",
                },
            )
    finally:
        importlib.reload(backend_main)

    assert response.status_code == 200


def test_runtime_auth_rejects_websocket_without_token(monkeypatch) -> None:
    monkeypatch.setenv("MINICODE_RUNTIME_TOKEN", "secret-token")

    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect("/ws?session_id=session_auth_missing") as ws:
                ws.receive_json()

    assert exc_info.value.code == 1008


def test_runtime_auth_rejects_websocket_query_token(monkeypatch) -> None:
    monkeypatch.setenv("MINICODE_RUNTIME_TOKEN", "secret-token")

    with TestClient(app) as client, pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(
            "/ws?session_id=session_auth_query&minicode_token=secret-token"
        ) as ws:
            ws.receive_json()

    assert exc_info.value.code == 1008


def test_websocket_llm_initialization_failure_is_terminal(monkeypatch) -> None:
    import backend.main as backend_main

    monkeypatch.delenv("MINICODE_RUNTIME_TOKEN", raising=False)
    monkeypatch.setattr(
        "backend.bootstrap.app.AppBootstrap.create_llm",
        lambda _bootstrap: (_ for _ in ()).throw(
            RuntimeError("provider is not configured")
        ),
    )

    with TestClient(backend_main.app) as client:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect("/ws?session_id=session_llm_init_failure") as ws:
                error = ws.receive_json()
                assert error["type"] == "error"
                assert error["recoverable"] is False
                assert error["error_code"] == "connection.llm_initialization_failed"
                ws.receive_json()

    assert exc_info.value.code == 1008


def test_runtime_auth_allows_websocket_token_subprotocol(monkeypatch) -> None:
    monkeypatch.setenv("MINICODE_RUNTIME_TOKEN", "secret-token")

    with TestClient(app) as client:
        with client.websocket_connect(
            "/ws?session_id=session_auth_ok",
            subprotocols=["minicode", "minicode-token.c2VjcmV0LXRva2Vu"],
        ) as ws:
            assert ws.accepted_subprotocol == "minicode"
            ws.send_json({"type": "ping"})
            payload = {}
            for _ in range(20):
                payload = ws.receive_json()
                if payload.get("type") == "pong":
                    break

    assert payload["type"] == "pong"


def test_runtime_auth_allows_authenticated_electron_file_origin(monkeypatch) -> None:
    monkeypatch.setenv("MINICODE_RUNTIME_TOKEN", "secret-token")

    with TestClient(app) as client:
        with client.websocket_connect(
            "/ws?session_id=session_auth_electron_file",
            headers={"Origin": "file://"},
            subprotocols=["minicode", "minicode-token.c2VjcmV0LXRva2Vu"],
        ) as ws:
            assert ws.accepted_subprotocol == "minicode"
            ws.send_json({"type": "ping"})
            payload = {}
            for _ in range(20):
                payload = ws.receive_json()
                if payload.get("type") == "pong":
                    break

    assert payload["type"] == "pong"


def test_runtime_auth_rejects_invalid_websocket_token_subprotocol(monkeypatch) -> None:
    monkeypatch.setenv("MINICODE_RUNTIME_TOKEN", "secret-token")

    with TestClient(app) as client, pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(
            "/ws?session_id=session_auth_invalid",
            subprotocols=["minicode", "minicode-token.not-valid-utf8_"],
        ) as ws:
            ws.receive_json()

    assert exc_info.value.code == 1008
