import mimetypes

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.workspace.api import create_workspace_router


@pytest.mark.parametrize("name,media_type", [
    ("logo#1.SVG", "image/svg+xml"),
    ("space logo.webp", "image/webp"),
    ("literal%20.pdf", "application/pdf"),
    ("report.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    ("report.rtf", "application/rtf"),
])
def test_raw_resources_use_web_media_types_and_revalidate_mutable_files(tmp_path, monkeypatch, name, media_type):
    monkeypatch.setattr("backend.workspace.trust.is_workspace_trusted", lambda _root: True)
    mimetypes.init()
    monkeypatch.setitem(mimetypes.types_map, ".svg", "image/svg")
    target = tmp_path / name
    target.write_bytes(b"first resource")
    app = FastAPI()
    app.include_router(create_workspace_router())
    with TestClient(app) as client:
        params = {"workspace_root": str(tmp_path), "path": name}
        response = client.get("/api/workspace/raw", params=params)
        assert response.status_code == 200
        assert response.headers["content-type"] == media_type
        assert response.headers["cache-control"] == "no-cache"
        assert response.content == b"first resource"
        target.write_bytes(b"new resource content")
        assert client.get("/api/workspace/raw", params=params).content == b"new resource content"
