from fastapi.testclient import TestClient
import httpx

from backend.main import app


def test_llm_check_reports_missing_custom_key_without_preset_success(monkeypatch) -> None:
    monkeypatch.delenv("CUSTOM_API_KEY", raising=False)
    monkeypatch.delenv("CUSTOM_ALLOW_OPENAI_KEY_FALLBACK", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://lucen.cc/v1")

    with TestClient(app) as client:
        response = client.post(
            "/api/llm/check",
            json={
                "provider": "custom",
                "custom": {
                    "base_url": "https://api.deepseek.com/v1",
                    "model": "deepseek-v4-flash",
                    "wire_api": "chat",
                },
                "confirm_sensitive_change": True,
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is False
    assert payload["provider_id"] == "custom"
    assert payload["has_api_key"] is False
    assert payload["generation_ok"] is False
    assert payload["failure_kind"] == "authentication_failed"
    assert payload["status_code"] == 401
    assert "Authentication" in payload["message"]


def test_llm_check_validates_selected_openai_compatible_model_generation(monkeypatch) -> None:
    seen: dict[str, str] = {}

    async def _models(base_url: str, api_key: str, **_kwargs: object) -> list[str]:
        seen["models_base_url"] = base_url
        seen["models_api_key"] = api_key
        return ["deepseek-v4-flash"]

    async def _generation(base_url: str, api_key: str, model: str, wire_api: str, **_kwargs: object) -> None:
        seen["generation_base_url"] = base_url
        seen["generation_api_key"] = api_key
        seen["generation_model"] = model
        seen["generation_wire_api"] = wire_api

    monkeypatch.setattr("backend.api.routes_llm._fetch_openai_compatible_models", _models)
    monkeypatch.setattr("backend.api.routes_llm._check_openai_compatible_generation", _generation)

    with TestClient(app) as client:
        response = client.post(
            "/api/llm/check",
            json={
                "provider": "custom",
                "custom": {
                    "api_key": "test-deepseek-key",
                    "base_url": "https://api.deepseek.com/v1",
                    "model": "deepseek-v4-flash",
                    "wire_api": "responses",
                },
                "confirm_sensitive_change": True,
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["provider_id"] == "custom"
    assert payload["wire_api"] == "responses"
    assert payload["message"] == "Provider connection and a small generation check succeeded."
    assert seen == {
        "models_base_url": "https://api.deepseek.com/v1",
        "models_api_key": "test-deepseek-key",
        "generation_base_url": "https://api.deepseek.com/v1",
        "generation_api_key": "test-deepseek-key",
        "generation_model": "deepseek-v4-flash",
        "generation_wire_api": "responses",
    }


def test_llm_check_reports_generation_probe_failure(monkeypatch) -> None:
    async def _models(base_url: str, api_key: str, **_kwargs: object) -> list[str]:
        return ["deepseek-v4-flash"]

    async def _generation(base_url: str, api_key: str, model: str, wire_api: str, **_kwargs: object) -> None:
        request = httpx.Request("POST", f"{base_url}/chat/completions")
        response = httpx.Response(404, request=request, text='{"error":{"message":"Not Found"}}')
        raise httpx.HTTPStatusError("404 Not Found", request=request, response=response)

    monkeypatch.setattr("backend.api.routes_llm._fetch_openai_compatible_models", _models)
    monkeypatch.setattr("backend.api.routes_llm._check_openai_compatible_generation", _generation)

    with TestClient(app) as client:
        response = client.post(
            "/api/llm/check",
            json={
                "provider": "custom",
                "custom": {
                    "api_key": "test-deepseek-key",
                    "base_url": "https://api.deepseek.com/v1",
                    "model": "deepseek-v4-flash",
                    "wire_api": "chat",
                },
                "confirm_sensitive_change": True,
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is False
    assert payload["provider_id"] == "custom"
    assert payload["status_code"] == 404
    assert "Not Found" in payload["message"]
    assert "API endpoint" in payload["hint"]


def test_llm_check_api_projects_explicit_direct_proxy_mode(monkeypatch) -> None:
    seen: list[tuple[str, str]] = []

    async def _models(
        _base_url: str,
        _api_key: str,
        *,
        proxy_mode: str,
        **_kwargs: object,
    ) -> list[str]:
        seen.append(("models", proxy_mode))
        return ["gateway-model"]

    async def _generation(
        _base_url: str,
        _api_key: str,
        _model: str,
        _wire_api: str,
        *,
        proxy_mode: str,
        **_kwargs: object,
    ) -> None:
        seen.append(("generation", proxy_mode))

    monkeypatch.setattr("backend.api.routes_llm._fetch_openai_compatible_models", _models)
    monkeypatch.setattr("backend.api.routes_llm._check_openai_compatible_generation", _generation)

    with TestClient(app) as client:
        response = client.post(
            "/api/llm/check",
            json={
                "provider": "custom",
                "custom": {
                    "api_key": "test-key",
                    "base_url": "https://gateway.example/v1",
                    "model": "gateway-model",
                    "wire_api": "chat",
                    "proxy_mode": "direct",
                    "image_mode": "disabled",
                },
                "confirm_sensitive_change": True,
            },
        )

    assert response.status_code == 200
    assert response.json()["proxy_mode"] == "direct"
    assert seen == [("models", "direct"), ("generation", "direct")]


def test_llm_check_api_rejects_unknown_proxy_mode() -> None:
    with TestClient(app) as client:
        response = client.post(
            "/api/llm/check",
            json={
                "provider": "custom",
                "custom": {
                    "api_key": "test-key",
                    "base_url": "https://gateway.example/v1",
                    "model": "gateway-model",
                    "wire_api": "chat",
                    "proxy_mode": "automatic-fallback",
                },
            },
        )

    assert response.status_code == 422
