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


def test_websocket_llm_initialization_failure_settles_query_and_keeps_transport(monkeypatch) -> None:
    import backend.main as backend_main
    from backend.agent.runtime import default_runtime

    monkeypatch.delenv("MINICODE_RUNTIME_TOKEN", raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_MODEL", "initialization-test-model")
    monkeypatch.setenv("OPENAI_AVAILABLE_MODELS", "initialization-test-model")
    adapter_calls = []

    def unavailable_adapter(*args, **kwargs):
        adapter_calls.append((args, kwargs))
        raise RuntimeError("provider is not configured")

    monkeypatch.setattr(backend_main, "_create_session_llm", unavailable_adapter)
    monkeypatch.setattr("backend.llm.model_registry.create_session_llm", unavailable_adapter)

    with TestClient(backend_main.app) as client:
        with client.websocket_connect("/ws?session_id=session_llm_init_failure") as ws:
            def receive(event_type):
                for _ in range(50):
                    payload = ws.receive_json()
                    if payload["type"] == event_type:
                        return payload
                pytest.fail(f"Expected {event_type} from the actual WebSocket protocol")

            receive("llm.model.updated")
            assert adapter_calls == []
            ws.send_json({"type": "conversation.create", "activate": True, "title": "Unavailable provider"})
            conversation_id = receive("conversation.switched")["conversation_id"]
            ws.send_json({"type": "user_message", "conversation_id": conversation_id, "content": "Inspect the task",
                "client_command_id": "initialization-failure-query", "user_message_id": "initialization-user",
                "assistant_message_id": "initialization-assistant"})
            error = receive("error")
            assert error["message"] and error["error_type"]
            assert error["conversation_id"] == conversation_id
            done = receive("done")
            assert done["status"] == "failed" and done["reason"] == "llm_initialization_failed"
            assert len(adapter_calls) == 1
            terminal = default_runtime().latest_main_run(conversation_id)
            assert terminal.status == "failed" and terminal.terminal_reason == "llm_initialization_failed"
            assert terminal.error == error["message"]
            ws.send_json({"type": "ping"})
            assert receive("pong")["type"] == "pong"


def test_websocket_session_initialization_failure_is_terminal(monkeypatch) -> None:
    import backend.main as backend_main

    monkeypatch.delenv("MINICODE_RUNTIME_TOKEN", raising=False)

    def unavailable_registry(*_args, **_kwargs):
        raise RuntimeError("session registry initialization failed")

    monkeypatch.setattr("backend.bootstrap.app.AppBootstrap.create_tool_registry", unavailable_registry)
    with TestClient(backend_main.app) as client:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect("/ws?session_id=session_llm_init_failure") as ws:
                error = ws.receive_json()
                assert error["type"] == "error"
                assert error["recoverable"] is False
                assert error["error_code"] == "connection.session_initialization_failed"
                ws.receive_json()

    assert exc_info.value.code == 1011


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
