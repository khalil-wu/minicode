"""Runtime authentication helpers for REST and WebSocket endpoints."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time

from fastapi import Request, WebSocket

RUNTIME_TOKEN_ENV = "MINICODE_RUNTIME_TOKEN"
WORKSPACE_RAW_TOKEN_TTL_SECONDS = 300


def _runtime_token() -> str:
    return os.environ.get(RUNTIME_TOKEN_ENV, "").strip()


def _constant_time_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def _request_runtime_token(request: Request) -> str:
    return request.headers.get("x-minicode-token", "").strip()


def _is_runtime_authorized(request: Request) -> bool:
    expected = _runtime_token()
    if not expected:
        # Keep tokenless desktop development usable locally, but never expose
        # runtime APIs (including settings and replay) to the LAN by default.
        client_host = str(request.client.host if request.client else "").strip().lower()
        return client_host in {"127.0.0.1", "::1", "localhost", "testclient", "testserver"}
    supplied = _request_runtime_token(request)
    return bool(supplied) and _constant_time_equal(supplied, expected)


def _websocket_protocol_runtime_token(websocket: WebSocket) -> str:
    header = websocket.headers.get("sec-websocket-protocol", "")
    for raw_protocol in header.split(","):
        protocol = raw_protocol.strip()
        if not protocol.startswith("minicode-token."):
            continue
        encoded = protocol.removeprefix("minicode-token.").strip()
        if not encoded:
            continue
        try:
            padded = encoded + ("=" * (-len(encoded) % 4))
            return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        except Exception:
            return ""
    return ""


def _websocket_accept_subprotocol(websocket: WebSocket) -> str | None:
    """Return the safe app subprotocol to echo during accept, if requested.

    The token-bearing protocol is intentionally not echoed. It is only a
    browser-compatible transport for the runtime token.
    """
    header = websocket.headers.get("sec-websocket-protocol", "")
    for raw_protocol in header.split(","):
        if raw_protocol.strip() == "minicode":
            return "minicode"
    return None


def _websocket_runtime_token(websocket: WebSocket) -> str:
    # Browser WebSocket API cannot set custom headers, so browser clients pass
    # the runtime token through a Sec-WebSocket-Protocol entry so it does not
    # appear in the URL.
    return (
        websocket.headers.get("x-minicode-token", "")
        or _websocket_protocol_runtime_token(websocket)
    ).strip()


def _is_websocket_authorized(websocket: WebSocket) -> bool:
    expected = _runtime_token()
    if not expected:
        client_host = str(websocket.client.host if websocket.client else "").strip().lower()
        return client_host in {"127.0.0.1", "::1", "localhost", "testclient", "testserver"}
    supplied = _websocket_runtime_token(websocket)
    return bool(supplied) and _constant_time_equal(supplied, expected)


def _workspace_raw_token_signature(path: str, expires_at: int, secret: str) -> str:
    payload = f"workspace_raw:v1:{path}:{expires_at}".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _build_workspace_raw_token(path: str, *, now: int | None = None) -> str:
    secret = _runtime_token()
    if not secret:
        return ""
    current_time = int(time.time() if now is None else now)
    expires_at = current_time + WORKSPACE_RAW_TOKEN_TTL_SECONDS
    signature = _workspace_raw_token_signature(path, expires_at, secret)
    return f"{expires_at}.{signature}"


def _is_workspace_raw_request(request: Request) -> bool:
    path = request.scope.get("path", "")
    return path in {"/api/workspace/raw", "/api/v1/workspace/raw"}


def _is_workspace_raw_token_authorized(request: Request) -> bool:
    secret = _runtime_token()
    if not secret or not _is_workspace_raw_request(request):
        return False
    raw_token = request.query_params.get("raw_token", "").strip()
    raw_path = request.query_params.get("path", "").strip()
    if not raw_token or not raw_path:
        return False
    expires_at_text, separator, supplied_signature = raw_token.partition(".")
    if not separator or not supplied_signature:
        return False
    try:
        expires_at = int(expires_at_text)
    except ValueError:
        return False
    if expires_at < int(time.time()):
        return False
    expected_signature = _workspace_raw_token_signature(raw_path, expires_at, secret)
    return _constant_time_equal(supplied_signature, expected_signature)
