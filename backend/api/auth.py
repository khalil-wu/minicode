"""Runtime authentication helpers for REST and WebSocket endpoints."""

from __future__ import annotations

import hmac
import os

from fastapi import Request, WebSocket

RUNTIME_TOKEN_ENV = "MINICODE_RUNTIME_TOKEN"


def _runtime_token() -> str:
    return os.environ.get(RUNTIME_TOKEN_ENV, "").strip()


def _constant_time_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def _request_runtime_token(request: Request) -> str:
    return (
        request.headers.get("x-minicode-token", "")
        or request.query_params.get("minicode_token", "")
    ).strip()


def _is_runtime_authorized(request: Request) -> bool:
    expected = _runtime_token()
    if not expected:
        return True
    supplied = _request_runtime_token(request)
    return bool(supplied) and _constant_time_equal(supplied, expected)


def _websocket_runtime_token(websocket: WebSocket) -> str:
    # Browser WebSocket API cannot set custom headers, so we fall back to query
    # params for dev-mode browser connections. In Electron desktop mode the token
    # is injected via preload and never appears in the URL. Access logs must not
    # record query strings containing the token.
    return (
        websocket.headers.get("x-minicode-token", "")
        or websocket.query_params.get("minicode_token", "")
    ).strip()


def _is_websocket_authorized(websocket: WebSocket) -> bool:
    expected = _runtime_token()
    if not expected:
        return True
    supplied = _websocket_runtime_token(websocket)
    return bool(supplied) and _constant_time_equal(supplied, expected)
