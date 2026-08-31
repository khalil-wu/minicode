import json

import pytest
from fastapi.testclient import TestClient

from backend.main import app
from backend.mcp.config_file import read_mcp_config, write_mcp_config


def test_read_mcp_config_returns_empty_document_when_file_is_missing(tmp_path) -> None:
    result = read_mcp_config(tmp_path / ".mcp.json")

    assert result["exists"] is False
    assert result["content"] == '{\n  "servers": {}\n}\n'
    assert result["servers"] == []


def test_write_mcp_config_rejects_invalid_json_without_touching_file(tmp_path) -> None:
    config_path = tmp_path / ".mcp.json"
    config_path.write_text('{"servers": {}}\n', encoding="utf-8")

    try:
      write_mcp_config("{bad json", config_path)
    except ValueError as exc:
      assert "Invalid JSON" in str(exc)
    else:
      raise AssertionError("write_mcp_config should reject invalid JSON")

    assert config_path.read_text(encoding="utf-8") == '{"servers": {}}\n'


def test_write_mcp_config_creates_backup_and_normalizes_valid_config(tmp_path) -> None:
    config_path = tmp_path / ".mcp.json"
    config_path.write_text(
        json.dumps({
            "servers": {
                "old": {
                    "transport": "stdio",
                    "command": "python",
                    "args": ["-m", "old"],
                }
            }
        }),
        encoding="utf-8",
    )

    result = write_mcp_config(
        json.dumps(
            {
                "servers": {
                    "docs": {
                        "transport": "stdio",
                        "command": "node",
                        "args": ["server.js"],
                        "env": {"TOKEN": "${DOCS_TOKEN}"},
                        "auto_start": True,
                    }
                }
            }
        ),
        config_path,
    )

    assert result["backup_path"]
    assert (tmp_path / result["backup_path"].split("/")[-1]).exists()
    assert result["servers"][0]["name"] == "docs"
    assert result["servers"][0]["transport"] == "stdio"
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["servers"]["docs"]["command"] == "node"


def test_write_mcp_config_persists_canonical_streamable_http_shape(tmp_path) -> None:
    config_path = tmp_path / ".mcp.json"

    write_mcp_config(
        json.dumps(
            {
                "servers": {
                    "figma-desktop": {
                        "transport": "http",
                        "url": "http://127.0.0.1:3845/mcp",
                    }
                }
            }
        ),
        config_path,
    )

    saved = json.loads(config_path.read_text(encoding="utf-8"))
    figma = saved["servers"]["figma-desktop"]
    assert figma["transport"] == "http"
    assert "type" not in figma
    assert figma["url"] == "http://127.0.0.1:3845/mcp"
    assert "command" not in figma


def test_write_mcp_config_preserves_explicit_sse_transport(tmp_path) -> None:
    config_path = tmp_path / ".mcp.json"

    write_mcp_config(
        json.dumps({
            "servers": {
                "events": {
                    "transport": "sse",
                    "url": "https://mcp.example/sse",
                }
            }
        }),
        config_path,
    )

    events = json.loads(config_path.read_text(encoding="utf-8"))["servers"]["events"]
    assert events["transport"] == "sse"
    assert "type" not in events


def test_mcp_config_api_round_trip(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / ".mcp.json"
    monkeypatch.setattr("backend.api.routes_llm.MCP_CONFIG_FILE", config_path)

    with TestClient(app) as client:
        get_response = client.get("/api/mcp/config")
        put_response = client.put(
            "/api/mcp/config",
            json={
                "content": json.dumps(
                    {
                        "servers": {
                            "browser": {
                                "transport": "http",
                                "url": "https://mcp.example/browser",
                                "auto_start": False,
                            }
                        }
                    }
                ),
                "reload": False,
                "confirm_sensitive_change": True,
            },
        )

    assert get_response.status_code == 200
    assert get_response.json()["exists"] is False
    assert put_response.status_code == 200
    payload = put_response.json()
    assert payload["saved"] is True
    assert payload["config"]["servers"][0]["name"] == "browser"
    assert payload["config"]["servers"][0]["transport"] == "http"

    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["servers"]["browser"]["transport"] == "http"
    assert "type" not in saved["servers"]["browser"]


def test_mcp_config_api_requires_explicit_confirmation(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / ".mcp.json"
    config_path.write_text('{"servers": {}}\n', encoding="utf-8")
    monkeypatch.setattr("backend.api.routes_llm.MCP_CONFIG_FILE", config_path)

    with TestClient(app) as client:
        response = client.put(
            "/api/mcp/config",
            json={
                "content": json.dumps(
                    {
                        "servers": {
                            "browser": {
                                "transport": "http",
                                "url": "https://mcp.example/browser",
                            }
                        }
                    }
                ),
                "reload": False,
            },
        )

    assert response.status_code == 409
    assert json.loads(config_path.read_text(encoding="utf-8")) == {"servers": {}}


@pytest.mark.parametrize("transport", ["", None, "sse-ide", "ws-ide"])
def test_write_mcp_config_rejects_blank_or_internal_transport(tmp_path, transport) -> None:
    config_path = tmp_path / ".mcp.json"

    with pytest.raises(ValueError, match="invalid transport"):
        write_mcp_config(
            json.dumps({
                "servers": {
                    "broken": {
                        "transport": transport,
                        "url": "https://mcp.example/mcp",
                    }
                }
            }),
            config_path,
        )

    assert not config_path.exists()


def test_write_mcp_config_rejects_non_minicode_transport_field(tmp_path) -> None:
    with pytest.raises(ValueError, match="unsupported fields: type"):
        write_mcp_config(
            json.dumps({
                "servers": {
                    "broken": {
                        "transport": "http",
                        "type": "sse",
                        "url": "https://mcp.example/mcp",
                    }
                }
            }),
            tmp_path / ".mcp.json",
        )


@pytest.mark.parametrize(
    "server, message",
    [
        ({"command": "node", "url": "https://mcp.example/mcp"}, "explicit transport"),
        ({}, "explicit transport"),
        ({"transport": "stdio", "command": "node", "url": "https://mcp.example/mcp"}, "not supported"),
        ({"transport": "stdio", "command": "node", "headers": {"X-Test": "1"}}, "not supported"),
        ({"transport": "http", "url": "https://mcp.example/mcp", "args": ["stale"]}, "not supported"),
        ({"transport": "ws", "url": "wss://mcp.example/ws", "oauth": {"client_id": "x"}}, "not supported"),
    ],
)
def test_write_mcp_config_rejects_ambiguous_or_incompatible_shapes(
    tmp_path,
    server,
    message,
) -> None:
    with pytest.raises(ValueError, match=message):
        write_mcp_config(
            json.dumps({"servers": {"broken": server}}),
            tmp_path / ".mcp.json",
        )
