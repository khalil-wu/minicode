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
SKILL_ASSET_TOKEN_TTL_SECONDS = 300
PLUGIN_ASSET_TOKEN_TTL_SECONDS = 300
ATTACHMENT_ASSET_TOKEN_TTL_SECONDS = 300
ARTIFACT_ASSET_TOKEN_TTL_SECONDS = 300


def _runtime_token() -> str:
    return os.environ.get(RUNTIME_TOKEN_ENV, "").strip()


def _runtime_token_configured() -> bool:
    return bool(_runtime_token())


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


def _workspace_raw_token_signature(path: str, workspace_root: str, expires_at: int, secret: str) -> str:
    payload = f"workspace_raw:v2:{workspace_root}:{path}:{expires_at}".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _build_workspace_raw_token(path: str, workspace_root: str = "", *, now: int | None = None) -> str:
    secret = _runtime_token()
    if not secret:
        return ""
    current_time = int(time.time() if now is None else now)
    expires_at = current_time + WORKSPACE_RAW_TOKEN_TTL_SECONDS
    signature = _workspace_raw_token_signature(path, workspace_root, expires_at, secret)
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
    workspace_root = request.query_params.get("workspace_root", "").strip()
    if not raw_token or not raw_path or not workspace_root:
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
    expected_signature = _workspace_raw_token_signature(raw_path, workspace_root, expires_at, secret)
    return _constant_time_equal(supplied_signature, expected_signature)


def _skill_asset_token_signature(skill_path: str, variant: str, expires_at: int, secret: str) -> str:
    payload = f"skill_asset:v1:{skill_path}:{variant}:{expires_at}".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _build_skill_asset_token(
    skill_path: str,
    variant: str,
    *,
    now: int | None = None,
) -> str:
    secret = _runtime_token()
    if not secret:
        return ""
    current_time = int(time.time() if now is None else now)
    expires_at = current_time + SKILL_ASSET_TOKEN_TTL_SECONDS
    signature = _skill_asset_token_signature(skill_path, variant, expires_at, secret)
    return f"{expires_at}.{signature}"


def _is_skill_asset_request(request: Request) -> bool:
    path = request.scope.get("path", "")
    return path in {"/api/skills/asset", "/api/v1/skills/asset"}


def _is_skill_asset_token_authorized(request: Request) -> bool:
    secret = _runtime_token()
    if not secret or not _is_skill_asset_request(request):
        return False
    asset_token = request.query_params.get("asset_token", "").strip()
    skill_path = request.query_params.get("skill_path", "").strip()
    variant = request.query_params.get("variant", "").strip().lower()
    if not asset_token or not skill_path or variant not in {"small", "large"}:
        return False
    expires_at_text, separator, supplied_signature = asset_token.partition(".")
    if not separator or not supplied_signature:
        return False
    try:
        expires_at = int(expires_at_text)
    except ValueError:
        return False
    if expires_at < int(time.time()):
        return False
    expected_signature = _skill_asset_token_signature(skill_path, variant, expires_at, secret)
    return _constant_time_equal(supplied_signature, expected_signature)


def _plugin_asset_token_signature(plugin_path: str, variant: str, expires_at: int, secret: str) -> str:
    payload = f"plugin_asset:v1:{plugin_path}:{variant}:{expires_at}".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _is_plugin_asset_token_authorized(request: Request) -> bool:
    secret = _runtime_token()
    path = request.scope.get("path", "")
    if not secret or path not in {"/api/plugins/asset", "/api/v1/plugins/asset"}:
        return False
    asset_token = request.query_params.get("asset_token", "").strip()
    plugin_path = request.query_params.get("plugin_path", "").strip()
    variant = request.query_params.get("variant", "").strip()
    if not asset_token or not plugin_path or variant not in {"composer", "logo", "logo-dark"}:
        return False
    expires_at_text, separator, supplied_signature = asset_token.partition(".")
    if not separator or not supplied_signature:
        return False
    try:
        expires_at = int(expires_at_text)
    except ValueError:
        return False
    if expires_at < int(time.time()):
        return False
    expected_signature = _plugin_asset_token_signature(plugin_path, variant, expires_at, secret)
    return _constant_time_equal(supplied_signature, expected_signature)


def _attachment_asset_token_signature(
    artifact_id: str,
    session_id: str,
    conversation_id: str,
    expires_at: int,
    secret: str,
) -> str:
    payload = (
        f"attachment_raw:v2:{session_id}:{conversation_id}:{artifact_id}:{expires_at}"
    ).encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _build_attachment_asset_token(
    artifact_id: str,
    session_id: str,
    conversation_id: str,
    *,
    now: int | None = None,
) -> str:
    secret = _runtime_token()
    if not secret:
        return ""
    current_time = int(time.time() if now is None else now)
    expires_at = current_time + ATTACHMENT_ASSET_TOKEN_TTL_SECONDS
    signature = _attachment_asset_token_signature(
        artifact_id,
        session_id,
        conversation_id,
        expires_at,
        secret,
    )
    return f"{expires_at}.{signature}"


def _is_attachment_asset_token_authorized(request: Request) -> bool:
    secret = _runtime_token()
    path = request.scope.get("path", "")
    if not secret or path not in {"/api/attachments/raw", "/api/v1/attachments/raw"}:
        return False
    asset_token = request.query_params.get("asset_token", "").strip()
    artifact_id = request.query_params.get("artifact_id", "").strip()
    session_id = request.query_params.get("session_id", "").strip()
    conversation_id = request.query_params.get("conversation_id", "").strip()
    if not asset_token or not artifact_id or not session_id or not conversation_id:
        return False
    expires_at_text, separator, supplied_signature = asset_token.partition(".")
    if not separator or not supplied_signature:
        return False
    try:
        expires_at = int(expires_at_text)
    except ValueError:
        return False
    if expires_at < int(time.time()):
        return False
    expected_signature = _attachment_asset_token_signature(
        artifact_id,
        session_id,
        conversation_id,
        expires_at,
        secret,
    )
    return _constant_time_equal(supplied_signature, expected_signature)


def _artifact_asset_token_signature(
    artifact_id: str,
    session_id: str,
    conversation_id: str,
    expires_at: int,
    secret: str,
) -> str:
    payload = (
        f"artifact_raw:v1:{session_id}:{conversation_id}:{artifact_id}:{expires_at}"
    ).encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _build_artifact_asset_token(
    artifact_id: str,
    session_id: str,
    conversation_id: str,
    *,
    now: int | None = None,
) -> str:
    secret = _runtime_token()
    if not secret:
        return ""
    current_time = int(time.time() if now is None else now)
    expires_at = current_time + ARTIFACT_ASSET_TOKEN_TTL_SECONDS
    signature = _artifact_asset_token_signature(
        artifact_id,
        session_id,
        conversation_id,
        expires_at,
        secret,
    )
    return f"{expires_at}.{signature}"


def _is_artifact_asset_token_authorized(request: Request) -> bool:
    secret = _runtime_token()
    path = request.scope.get("path", "")
    if not secret or path not in {"/api/artifacts/raw", "/api/v1/artifacts/raw"}:
        return False
    asset_token = request.query_params.get("asset_token", "").strip()
    artifact_id = request.query_params.get("artifact_id", "").strip()
    session_id = request.query_params.get("session_id", "").strip()
    conversation_id = request.query_params.get("conversation_id", "").strip()
    if not asset_token or not artifact_id or not session_id or not conversation_id:
        return False
    expires_at_text, separator, supplied_signature = asset_token.partition(".")
    if not separator or not supplied_signature:
        return False
    try:
        expires_at = int(expires_at_text)
    except ValueError:
        return False
    if expires_at < int(time.time()):
        return False
    expected_signature = _artifact_asset_token_signature(
        artifact_id,
        session_id,
        conversation_id,
        expires_at,
        secret,
    )
    return _constant_time_equal(supplied_signature, expected_signature)
