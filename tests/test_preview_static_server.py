from pathlib import Path

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
