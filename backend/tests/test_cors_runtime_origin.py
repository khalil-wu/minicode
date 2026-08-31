from __future__ import annotations

from backend import main


def test_runtime_frontend_url_is_normalized_to_browser_origin(monkeypatch) -> None:
    monkeypatch.delenv("MINICODE_CORS_ORIGINS", raising=False)
    monkeypatch.setenv("MINICODE_FRONTEND_URL", "http://127.0.0.1:43173/workbench/?test=1")

    origins = main._build_cors_origins()

    assert "http://127.0.0.1:43173" in origins
    assert "http://127.0.0.1:43173/workbench/?test=1" not in origins
