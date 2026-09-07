import http.client
import os
import threading
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from backend.preview.static_server import PreviewRequestHandler


def _request_handler(root: Path, request_path: str) -> PreviewRequestHandler:
    handler = object.__new__(PreviewRequestHandler)
    handler._preview_root = root.resolve()
    handler._access_token = "preview-token"
    handler.path = request_path
    return handler


def test_preview_static_server_rejects_encoded_nul_without_raising(tmp_path: Path) -> None:
    handler = _request_handler(tmp_path, "/preview-token/%00index.html")

    assert handler._requested_file() is None


def test_preview_static_server_still_resolves_valid_token_scoped_file(tmp_path: Path) -> None:
    target = tmp_path / "index.html"
    target.write_text("<h1>preview</h1>", encoding="utf-8")
    handler = _request_handler(tmp_path, "/preview-token/index.html")

    assert handler._requested_file() == target.resolve()


@pytest.fixture
def static_http_server(tmp_path: Path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(
        PreviewRequestHandler, directory=str(tmp_path), access_token="preview-token",
    ))
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    yield server
    server.shutdown()
    server.server_close()
    worker.join(timeout=5)


def _request(server: ThreadingHTTPServer, path: str, method: str = "GET"):
    client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
    try:
        client.request(method, path)
        response = client.getresponse()
        return response.status, int(response.getheader("Content-Length")), response.read()
    finally:
        client.close()


def _symlink(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except OSError as error:
        if os.name == "nt" and error.winerror == 1314:
            pytest.skip("Windows symlink privilege is unavailable")
        raise


@pytest.mark.parametrize("target_kind", ["file", "directory"])
def test_public_alias_cannot_serve_hidden_workspace_files(tmp_path: Path, static_http_server, target_kind: str) -> None:
    secret_directory = tmp_path / ".private"
    secret_directory.mkdir()
    secret = secret_directory / "credentials.txt"
    secret.write_text("AUDIT_SECRET_MARKER", encoding="utf-8")
    if target_kind == "file":
        _symlink(tmp_path / "public.txt", secret)
        path = "/preview-token/public.txt"
    else:
        _symlink(tmp_path / "assets", secret_directory)
        path = "/preview-token/assets/credentials.txt"

    status, _, body = _request(static_http_server, path)

    assert status == 404
    assert b"AUDIT_SECRET_MARKER" not in body


def test_hidden_alias_is_rejected_even_when_target_is_public(tmp_path: Path) -> None:
    target = tmp_path / "public.txt"
    target.write_text("public", encoding="utf-8")
    _symlink(tmp_path / ".hidden", target)

    assert _request_handler(tmp_path, "/preview-token/.hidden")._requested_file() is None


def test_public_alias_inside_preview_root_still_works(tmp_path: Path, static_http_server) -> None:
    target = tmp_path / "public.txt"
    target.write_bytes(b"public")
    _symlink(tmp_path / "alias.txt", target)

    assert _request(static_http_server, "/preview-token/alias.txt") == (200, 6, b"public")


@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize("replacement_body", [b"X", b"X" * 128], ids=["shorter", "longer"])
@pytest.mark.skipif(os.name == "nt", reason="Windows denies replacing this open Python file handle")
def test_http_headers_and_body_use_the_same_open_file_snapshot(
    tmp_path: Path, static_http_server, monkeypatch, method: str, replacement_body: bytes,
) -> None:
    target = tmp_path / "index.html"
    original_body = "旧的预览内容\r\n".encode("utf-8")
    target.write_bytes(original_body)
    replacement = tmp_path / "replacement.html"
    replacement.write_bytes(replacement_body)
    original_open = Path.open

    def open_then_replace(path: Path, *args, **kwargs):
        handle = original_open(path, *args, **kwargs)
        if path == target and args == ("rb",):
            replacement.replace(target)
        return handle

    monkeypatch.setattr(Path, "open", open_then_replace)

    status, content_length, body = _request(static_http_server, "/preview-token/index.html", method)

    assert status == 200
    assert content_length == len(original_body)
    assert body == (original_body if method == "GET" else b"")
    assert target.read_bytes() == replacement_body
