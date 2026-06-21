"""OAuth 2.1 + PKCE support for HTTP MCP servers (MCP authorization spec).

Holds the testable core of the OAuth flow: PKCE challenge generation, token
parsing, refresh logic, and per-server token persistence. The browser +
loopback-redirect interaction (env-dependent) lives in the manager; this module
stays pure so it can be unit-tested without a live authorization server.

The on-the-wire integration (Authorization header, 401 → refresh + retry) is in
``mcp.client.MCPClient``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode


@dataclass
class OAuthTokens:
    """Access + refresh tokens for one MCP server."""

    access_token: str
    refresh_token: str = ""
    expires_at: float = 0.0  # epoch seconds; 0 = no known expiry
    token_type: str = "Bearer"

    def is_expired(self, *, skew_seconds: float = 30.0) -> bool:
        """True if the access token should be treated as expired (with skew)."""
        return bool(self.expires_at) and time.time() >= (self.expires_at - skew_seconds)

    def authorization_header(self) -> str:
        """The value for the HTTP Authorization header."""
        scheme = self.token_type or "Bearer"
        # Normalize "bearer"/"BEARER" → "Bearer" for the header.
        scheme = scheme[:1].upper() + scheme[1:].lower() if scheme else "Bearer"
        return f"{scheme} {self.access_token}"


def generate_pkce_pair() -> tuple[str, str]:
    """Return ``(code_verifier, code_challenge)`` using S256.

    The verifier is a 43+ char URL-safe random string; the challenge is the
    base64url SHA-256 of it (RFC 7636 §4.2).
    """
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def build_authorization_url(
    authorization_endpoint: str,
    *,
    client_id: str,
    redirect_uri: str,
    code_challenge: str,
    state: str,
    scope: str = "",
) -> str:
    """Build the authorization-endpoint URL with PKCE params."""
    params: dict[str, str] = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    if scope:
        params["scope"] = scope
    sep = "&" if "?" in authorization_endpoint else "?"
    return f"{authorization_endpoint}{sep}{urlencode(params)}"


def parse_token_response(data: dict[str, Any]) -> OAuthTokens:
    """Parse a token-endpoint JSON response into OAuthTokens.

    Raises ValueError if the response has no access_token.
    """
    access = str(data.get("access_token", "")).strip()
    if not access:
        raise ValueError("token response missing access_token")
    raw_expires = data.get("expires_in")
    try:
        expires_at = time.time() + float(raw_expires) if raw_expires is not None else 0.0
    except (TypeError, ValueError):
        expires_at = 0.0
    return OAuthTokens(
        access_token=access,
        refresh_token=str(data.get("refresh_token", "")).strip(),
        expires_at=expires_at,
        token_type=str(data.get("token_type", "Bearer")).strip() or "Bearer",
    )


class TokenStore:
    """Per-server OAuth token persistence (JSON file)."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self, data: dict[str, dict[str, Any]]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def get(self, server: str) -> OAuthTokens | None:
        row = self._load().get(server)
        if not row or not row.get("access_token"):
            return None
        try:
            return OAuthTokens(
                access_token=str(row["access_token"]),
                refresh_token=str(row.get("refresh_token", "")),
                expires_at=float(row.get("expires_at", 0.0) or 0.0),
                token_type=str(row.get("token_type", "Bearer") or "Bearer"),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def set(self, server: str, tokens: OAuthTokens) -> None:
        data = self._load()
        data[server] = {
            "access_token": tokens.access_token,
            "refresh_token": tokens.refresh_token,
            "expires_at": tokens.expires_at,
            "token_type": tokens.token_type,
        }
        self._save(data)

    def clear(self, server: str) -> None:
        data = self._load()
        if server in data:
            data.pop(server, None)
            self._save(data)


def build_refresh_request_body(
    *,
    refresh_token: str,
    client_id: str,
    scope: str = "",
) -> dict[str, str]:
    """Build the form body for a refresh_token grant."""
    body = {"grant_type": "refresh_token", "refresh_token": refresh_token}
    if client_id:
        body["client_id"] = client_id
    if scope:
        body["scope"] = scope
    return body
