from __future__ import annotations

import json
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient
import pytest
from starlette.websockets import WebSocket

from backend import main


@pytest.mark.parametrize(("host", "origin", "allowed"), [
    ("127.0.0.1:18765", "http://127.0.0.1:18765", True),
    ("localhost:18765", "http://localhost:18765", True),
    ("[::1]:18765", "http://[::1]:18765", True),
    ("127.0.0.1:18765", "http://127.0.0.1:18766", False),
    ("127.0.0.1:18765", "https://external.example", False),
    ("external.example:18765", "http://external.example:18765", False),
])
def test_loopback_frontend_origin_tracks_its_actual_server_port(monkeypatch, host, origin, allowed) -> None:
    for name in ("MINICODE_CORS_ORIGINS", "MINICODE_FRONTEND_URL", "VITE_DEV_FRONTEND_ORIGIN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MINICODE_DISABLE_DEV_CORS", "1")
    websocket = WebSocket({
        "type": "websocket", "scheme": "ws", "path": "/ws", "query_string": b"",
        "server": ("127.0.0.1", 18765), "client": ("127.0.0.1", 50123),
        "headers": [(b"host", host.encode()), (b"origin", origin.encode())],
    }, receive=AsyncMock(), send=AsyncMock())

    assert main._websocket_origin_allowed(websocket) is allowed


def test_same_origin_production_frontend_can_connect_on_a_custom_port(monkeypatch) -> None:
    monkeypatch.delenv("MINICODE_RUNTIME_TOKEN", raising=False)
    monkeypatch.setenv("MINICODE_DISABLE_DEV_CORS", "1")
    with TestClient(main.app) as client:
        with client.websocket_connect(
            "ws://127.0.0.1:18765/ws?session_id=session_custom_port_audit",
            headers={"Origin": "http://127.0.0.1:18765"},
        ) as websocket:
            websocket.send_json({"type": "ping"})
            while (event := websocket.receive_json())["type"] != "pong":
                assert event["type"] != "error", event


@pytest.mark.parametrize("production", [True, False])
def test_theme_boot_script_is_available_in_both_frontend_modes(monkeypatch, tmp_path, production) -> None:
    monkeypatch.setattr(main, "IS_PRODUCTION", production)
    monkeypatch.setattr(main, "FRONTEND_DIST", tmp_path / "dist")
    monkeypatch.setattr(main, "FRONTEND_SRC", tmp_path / "frontend")
    directory = tmp_path / "dist" if production else tmp_path / "frontend" / "public"
    directory.mkdir(parents=True)
    script = "document.documentElement.dataset.theme = 'light';"
    (directory / "theme-boot.js").write_text(script, encoding="utf-8")

    response = TestClient(main.app).get("/theme-boot.js")

    assert response.status_code == 200
    assert response.text == script
    assert response.headers["content-type"].startswith("application/javascript")


def test_ui_preferences_use_the_configured_runtime_data_root(monkeypatch, tmp_path) -> None:
    state_data = tmp_path / "isolated-state" / "data"
    monkeypatch.setattr(main, "DATA_ROOT", state_data)
    monkeypatch.chdir(tmp_path)
    client = TestClient(main.app)

    response = client.put("/api/ui/preferences?session_id=session_audit", json={"sidebar_width": 420})

    assert response.status_code == 200
    saved = state_data / "ui_preferences" / "ui_prefs_session_audit.json"
    assert json.loads(saved.read_text(encoding="utf-8"))["sidebar_width"] == 420
    assert client.get("/api/ui/preferences?session_id=session_audit").json()["sidebar_width"] == 420
    assert not (tmp_path / "data" / "ui_preferences").exists()
