from fastapi.testclient import TestClient

from backend.main import app


def test_removed_provider_check_endpoint_cannot_generate_a_probe():
    with TestClient(app) as client:
        response = client.post("/api/llm/check", json={"provider": "custom"})

    assert response.status_code == 404
