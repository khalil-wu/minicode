"""Token-scoped static preview server with a workspace-secret denylist."""
from __future__ import annotations

import argparse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from backend.security.sensitive_files import is_protected_write_path, is_sensitive_file


class PreviewRequestHandler(SimpleHTTPRequestHandler):
    """Keep stdlib HTTP semantics while fencing every request to one token/root."""

    server_version = "MiniCodePreview/1"

    def __init__(self, *args, directory: str, access_token: str, **kwargs) -> None:
        self._preview_root = Path(directory).resolve()
        self._access_token = access_token
        super().__init__(*args, directory=directory, **kwargs)

    def _requested_file(self) -> Path | None:
        decoded = unquote(urlsplit(self.path).path)
        prefix = f"/{self._access_token}/"
        if not decoded.startswith(prefix):
            return None
        relative_text = decoded[len(prefix):]
        relative = Path(relative_text)
        if not relative_text or relative.is_absolute() or ".." in relative.parts:
            return None
        if any(part.startswith(".") for part in relative.parts):
            return None
        if is_sensitive_file(relative) or is_protected_write_path(relative):
            return None
        candidate = (self._preview_root / relative).resolve()
        try:
            candidate.relative_to(self._preview_root)
        except ValueError:
            return None
        return candidate if candidate.is_file() else None

    def send_head(self):  # type: ignore[no-untyped-def]
        target = self._requested_file()
        if target is None:
            self.send_error(404, "Preview resource not found")
            return None
        content_type = self.guess_type(str(target))
        try:
            handle = target.open("rb")
        except OSError:
            self.send_error(404, "Preview resource not found")
            return None
        try:
            stat = target.stat()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(stat.st_size))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            return handle
        except Exception:
            handle.close()
            raise

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--root", required=True)
    parser.add_argument("--token", required=True)
    args = parser.parse_args()
    root = str(Path(args.root).resolve())

    def handler(*handler_args, **handler_kwargs):  # type: ignore[no-untyped-def]
        return PreviewRequestHandler(
            *handler_args,
            directory=root,
            access_token=args.token,
            **handler_kwargs,
        )

    with ThreadingHTTPServer(("127.0.0.1", args.port), handler) as server:
        print(f"Serving static preview at http://127.0.0.1:{args.port}/", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
