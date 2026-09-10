import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.workspace.api import create_workspace_router


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr("backend.workspace.trust.is_workspace_trusted", lambda _root: True)
    app = FastAPI()
    app.include_router(create_workspace_router())
    with TestClient(app) as client:
        yield client


@pytest.mark.parametrize("directory", [False, True], ids=["file", "directory"])
def test_renamed_file_keeps_its_save_baseline(client, tmp_path, directory):
    source = tmp_path / "src" / "before.txt"
    source.parent.mkdir()
    source.write_bytes("原内容\r\n".encode("utf-8"))
    params = {"workspace_root": str(tmp_path)}
    snapshot = client.get("/api/workspace/file", params={**params, "path": "src/before.txt"}).json()
    before, after = ("src", "renamed") if directory else ("src/before.txt", "src/after.txt")
    response = client.post("/api/workspace/rename", params=params, json={"path": before, "new_path": after})
    assert response.status_code == 200
    assert response.json()["path"] == after
    new_file = "renamed/before.txt" if directory else "src/after.txt"
    save = client.put("/api/workspace/file/compare-write", params=params, json={
        "path": new_file, "expected_hash": snapshot["content_hash"], "content": "用户草稿\r\n",
    })
    assert save.status_code == 200
    assert (tmp_path / new_file).read_bytes() == "用户草稿\r\n".encode("utf-8")
    assert not source.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows case-insensitive rename")
def test_case_only_rename_changes_the_directory_entry(client, tmp_path):
    (tmp_path / "before.txt").write_bytes(b"content")
    response = client.post("/api/workspace/rename", params={"workspace_root": str(tmp_path)}, json={"path": "before.txt", "new_path": "BEFORE.txt"})
    assert response.status_code == 200
    assert response.json()["path"] == "BEFORE.txt"
    assert [path.name for path in tmp_path.iterdir()] == ["BEFORE.txt"]
    assert (tmp_path / "BEFORE.txt").read_bytes() == b"content"


@pytest.mark.parametrize("hardlink", [False, True], ids=["different-file", "hardlink"])
def test_existing_destination_is_not_overwritten(client, tmp_path, hardlink):
    source = tmp_path / "source.txt"
    destination = tmp_path / "destination.txt"
    source.write_bytes(b"source")
    if hardlink:
        destination.hardlink_to(source)
    else:
        destination.write_bytes(b"destination")
    response = client.post("/api/workspace/rename", params={"workspace_root": str(tmp_path)}, json={"path": "source.txt", "new_path": "destination.txt"})
    assert response.status_code == 409
    assert source.read_bytes() == b"source"
    assert destination.read_bytes() == (b"source" if hardlink else b"destination")


@pytest.mark.skipif(os.name != "nt", reason="Windows junction")
def test_junction_rename_reports_the_entry_without_following_its_target(client, tmp_path):
    import _winapi

    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (outside / "marker.txt").write_bytes(b"fixture content")
    _winapi.CreateJunction(str(outside), str(workspace / "link"))
    response = client.post("/api/workspace/rename", params={"workspace_root": str(workspace)}, json={"path": "link", "new_path": "renamed-link"})
    assert response.status_code == 200
    assert response.json()["path"] == "renamed-link"
    assert response.json()["is_dir"] is True
    assert not (workspace / "link").exists()
    assert (workspace / "renamed-link").resolve() == outside
    assert (outside / "marker.txt").read_bytes() == b"fixture content"
