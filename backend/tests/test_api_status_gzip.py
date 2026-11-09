"""Only the capability directory is compressed; other routes keep their exact bytes."""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.status_gzip import StatusGzipMiddleware


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(StatusGzipMiddleware, minimum_size=1024)
    payload = {"tools": [{"name": f"tool_{index}", "description": "x" * 200} for index in range(200)]}

    @app.get("/api/status")
    async def status() -> dict:
        return payload

    @app.get("/api/other")
    async def other() -> dict:
        return payload

    return app


def test_status_is_gzipped_for_clients_that_accept_it():
    with TestClient(_app()) as client:
        response = client.get("/api/status", headers={"Accept-Encoding": "gzip"})
        assert response.status_code == 200
        assert response.headers.get("content-encoding") == "gzip"
        assert "Accept-Encoding" in response.headers.get("vary", "")
        assert int(response.headers["content-length"]) < len(response.content)
        assert len(response.json()["tools"]) == 200


def test_other_routes_and_identity_clients_keep_exact_bytes():
    with TestClient(_app()) as client:
        other = client.get("/api/other", headers={"Accept-Encoding": "gzip"})
        assert other.status_code == 200 and "content-encoding" not in other.headers
        identity = client.get("/api/status", headers={"Accept-Encoding": "identity"})
        assert identity.status_code == 200 and "content-encoding" not in identity.headers
