"""UI-selected remote files enter the existing host-owned attachment store."""
from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
import secrets
import threading
from types import SimpleNamespace

import httpx
import pytest
from websockets.asyncio.client import connect

from backend.tests.test_desktop_remote_host import _daemon, _event, _native


@pytest.mark.asyncio
async def test_actual_remote_import_keeps_original_owner_bytes_and_native_window_boundary(tmp_path):
    workspace = tmp_path / "remote workspace"
    other_workspace = tmp_path / "other workspace"
    workspace.mkdir()
    other_workspace.mkdir()
    document = tmp_path / "outside-workspace 中文.txt"
    document.write_text("Selected on the actual remote host.\n中文原文", encoding="utf-8")
    image = tmp_path / "remote-image.png"
    image_bytes = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")
    image.write_bytes(image_bytes)
    oversized = tmp_path / "oversized.txt"
    with oversized.open("wb") as stream:
        stream.truncate(50 * 1024 * 1024 + 1)
    async with _daemon(tmp_path) as (first, _attach, windows, _state):
        window = {"window_instance_id": "remote-attachment-window", "window_owner_id": "primary", "window_binding_token": secrets.token_hex(16)}
        windows[window["window_instance_id"]] = window
        ready = first.ready
        session_id = "owned_remote_attachment_session"
        endpoint = (f"ws://127.0.0.1:{ready['backend_port']}/ws?session_id={session_id}"
            f"&window_instance_id={window['window_instance_id']}&window_owner_id={window['window_owner_id']}")
        protocols = ["minicode", "minicode-window." + window["window_binding_token"]]
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{ready['backend_port']}",
                headers={"x-minicode-token": ready["runtime_token"]}, timeout=30) as client:
            for root in (workspace, other_workspace):
                assert (await _native(client, window, "workspace:trust", [str(root)])).status_code == 200
            listing = await _native(client, window, "workspace:directoryEntries", [str(tmp_path), False])
            assert any(entry["path"] == str(document) and entry["type"] == "file" for entry in listing.json()["entries"])
            async with connect(endpoint, subprotocols=protocols, additional_headers={"x-minicode-token": ready["runtime_token"]}) as socket:
                await socket.send(json.dumps({"type": "workspace.set", "path": str(workspace)}))
                original = await _event(socket, "command.result", command="workspace.set")
                assert original["level"] == "success", original
                owner = original["data"]["conversation_id"]
                await socket.send(json.dumps({"type": "workspace.set", "path": str(other_workspace)}))
                switched = await _event(socket, "command.result", command="workspace.set")
                other_owner = switched["data"]["conversation_id"]
                assert other_owner != owner
                source = {"path": str(document), "sessionId": session_id, "conversationId": owner, "workspaceRoot": str(workspace)}
                imported = await _native(client, window, "attachments:importRemoteFile", [source])
                assert imported.status_code == 200, imported.text
                result = imported.json()
                assert result["conversation_id"] == owner
                assert result["source"] == {"path": str(document.resolve()), "name": document.name,
                    "mediaType": "text/plain", "sizeBytes": document.stat().st_size}
                assert "data" not in result["attachment"]
                assert result["attachment"]["source_path"] == str(document.resolve())
                query = {"session_id": session_id, "conversation_id": owner, "artifact_id": result["artifact_id"]}
                preview = await client.get("/api/attachments/preview", params=query)
                assert preview.status_code == 200, preview.text
                assert "中文原文" in preview.json()["content"]
                assert (await client.get("/api/attachments/preview", params={**query, "conversation_id": other_owner})).status_code == 404
                image_result = await _native(client, window, "attachments:importRemoteFile", [{**source, "path": str(image)}])
                assert image_result.status_code == 200, image_result.text
                image_payload = image_result.json()
                assert image_payload["source"]["mediaType"] == "image/png"
                assert "data" not in image_payload["attachment"]
                raw = await client.get("/api/attachments/raw", params={**query, "artifact_id": image_payload["artifact_id"]})
                assert raw.status_code == 200 and raw.content == image_bytes
                for path, status in ((workspace, 409), (tmp_path / "missing.txt", 404), (oversized, 413)):
                    rejected = await _native(client, window, "attachments:importRemoteFile", [{**source, "path": str(path)}])
                    assert rejected.status_code == status, rejected.text
                forged = {**window, "window_binding_token": "different-window"}
                assert (await _native(client, forged, "attachments:importRemoteFile", [source])).status_code == 403
                other_window = {**window, "window_instance_id": "another-valid-window"}
                windows[other_window["window_instance_id"]] = other_window
                assert (await _native(client, other_window, "attachments:importRemoteFile", [source])).status_code == 403
        await first.detach()


@pytest.mark.asyncio
async def test_cancelled_remote_import_keeps_reservation_until_actual_worker_finishes(tmp_path, monkeypatch):
    from backend.desktop_remote import native as module

    entered, continue_write, released = threading.Event(), threading.Event(), threading.Event()
    selected = tmp_path / "selected.txt"
    selected.write_bytes(b"original selected bytes")
    receipt = tmp_path / "worker-finished.txt"
    context = SimpleNamespace(release=lambda: released.set())
    session = SimpleNamespace(window_instance_id="original-window", window_owner_id="primary")
    monkeypatch.setattr(module._state, "ws_manager", SimpleNamespace(get_session=lambda session_id: session))
    monkeypatch.setattr(module, "get_attachment_store", lambda: object())
    monkeypatch.setattr(module, "reserve_attachment_upload_context", lambda **kwargs: context)

    def ingest(*, context, file_name, raw_content, source_path):
        assert raw_content == selected.read_bytes() and file_name == selected.name
        assert source_path == str(selected.resolve())
        entered.set()
        assert continue_write.wait(5)
        receipt.write_bytes(raw_content)
        return {"attachment": {"media_type": "text/plain"}, "conversation_id": "original-owner"}

    monkeypatch.setattr(module, "upload_document_payload", ingest)
    native = module.RemoteNative(lambda _event: None, "http://127.0.0.1:1", "unused-task-owned-token")
    request = module.NativeRequest(window_instance_id="original-window", window_owner_id="primary",
        window_binding_token="original-binding", channel="minicode:attachments:importRemoteFile", args=[])
    task = asyncio.create_task(native._import_attachment(request, {"path": str(selected), "sessionId": "original-session",
        "conversationId": "original-owner", "workspaceRoot": str(tmp_path)}))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not released.is_set() and not receipt.exists()
    finally:
        continue_write.set()
        assert await asyncio.to_thread(released.wait, 5)
        await native.close()
    assert receipt.read_bytes() == selected.read_bytes()
