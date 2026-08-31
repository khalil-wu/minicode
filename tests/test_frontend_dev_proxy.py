import importlib

import httpx
from fastapi.testclient import TestClient


def test_dev_index_supports_react_refresh_proxy(monkeypatch, tmp_path) -> None:
    import backend.config as backend_config
    import backend.main as backend_main

    frontend_root = tmp_path / "frontend"
    frontend_root.mkdir()
    (frontend_root / "index.html").write_text("<!doctype html><div id='root'></div>", encoding="utf-8")

    original_project_root = backend_config.PROJECT_ROOT
    monkeypatch.setattr(backend_config, "PROJECT_ROOT", tmp_path)

    reloaded_main = importlib.reload(backend_main)

    class _FakeViteClient:
        def __init__(self) -> None:
            self.paths: list[str] = []

        async def get(self, path: str) -> httpx.Response:
            self.paths.append(path)
            return httpx.Response(
                200,
                content=b"refresh-runtime",
                headers={"content-type": "application/javascript"},
            )

    fake_vite_client = _FakeViteClient()
    monkeypatch.setattr(reloaded_main, "_vite_client", fake_vite_client)

    try:
        assert reloaded_main.IS_PRODUCTION is False

        with TestClient(reloaded_main.app) as client:
            response = client.get("/@react-refresh")
    finally:
        monkeypatch.setattr(backend_config, "PROJECT_ROOT", original_project_root)
        importlib.reload(backend_main)

    assert response.status_code == 200
    assert response.text == "refresh-runtime"
    assert fake_vite_client.paths == ["/@react-refresh"]
