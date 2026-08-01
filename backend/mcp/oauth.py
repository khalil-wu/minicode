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
import os
import secrets
import time
import asyncio
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import keyring
from keyring.errors import KeyringError, PasswordDeleteError


class MCPAuthenticationRequired(ConnectionError):
    """A remote MCP server requires an explicit user-initiated OAuth login."""

    mcp_auth_required = True
    mcp_auth_expired = False

    def __init__(self, authorization_url: str = "") -> None:
        super().__init__("Authentication required; sign in from the Connectors settings.")
        self.authorization_url = authorization_url


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
    """Per-server OAuth persistence in the OS credential store."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._service = f"minicode-mcp:{hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:20]}"

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self, data: dict[str, dict[str, Any]]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Restrict the token file to the owner. These are live bearer + refresh
        # credentials; a world-readable JSON is a standing exfiltration target.
        # chmod is a no-op / best-effort on Windows (POSIX perms don't apply),
        # so guard it and never let a perms failure lose the token write.
        try:
            os.chmod(self._path.parent, 0o700)
        except OSError:
            pass
        self._path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        try:
            os.chmod(self._path, 0o600)
        except OSError:
            pass

    def get(self, server: str) -> OAuthTokens | None:
        try:
            secret = keyring.get_password(self._service, f"{server}:legacy")
        except KeyringError:
            secret = None
        legacy_data = self._load() if not secret else {}
        try:
            row = json.loads(secret) if secret else legacy_data.get(server)
        except (TypeError, ValueError):
            row = None
        if not row or not row.get("access_token"):
            return None
        try:
            tokens = OAuthTokens(
                access_token=str(row["access_token"]),
                refresh_token=str(row.get("refresh_token", "")),
                expires_at=float(row.get("expires_at", 0.0) or 0.0),
                token_type=str(row.get("token_type", "Bearer") or "Bearer"),
            )
            if not secret:
                self.set(server, tokens)
                legacy_data.pop(server, None)
                self._save(legacy_data)
            return tokens
        except (KeyError, TypeError, ValueError):
            return None

    def set(self, server: str, tokens: OAuthTokens) -> None:
        payload = {
            "access_token": tokens.access_token,
            "refresh_token": tokens.refresh_token,
            "expires_at": tokens.expires_at,
            "token_type": tokens.token_type,
        }
        keyring.set_password(self._service, f"{server}:legacy", json.dumps(payload))

    def clear(self, server: str) -> None:
        for suffix in ("legacy", "tokens", "client"):
            try:
                keyring.delete_password(self._service, f"{server}:{suffix}")
            except PasswordDeleteError:
                pass

    def sdk_storage(self, server: str) -> "SDKTokenStorage":
        return SDKTokenStorage(self._service, server)


class SDKTokenStorage:
    """Adapter for the official MCP SDK TokenStorage protocol."""

    def __init__(self, service: str, server: str) -> None:
        self._service = service
        self._server = server

    async def _get(self, suffix: str) -> str | None:
        return await asyncio.to_thread(
            keyring.get_password,
            self._service,
            f"{self._server}:{suffix}",
        )

    async def _set(self, suffix: str, value: str) -> None:
        await asyncio.to_thread(
            keyring.set_password,
            self._service,
            f"{self._server}:{suffix}",
            value,
        )

    async def get_tokens(self) -> Any | None:
        from mcp.shared.auth import OAuthToken

        raw = await self._get("tokens")
        return OAuthToken.model_validate_json(raw) if raw else None

    async def set_tokens(self, tokens: Any) -> None:
        await self._set("tokens", tokens.model_dump_json())

    async def get_client_info(self) -> Any | None:
        from mcp.shared.auth import OAuthClientInformationFull

        raw = await self._get("client")
        return OAuthClientInformationFull.model_validate_json(raw) if raw else None

    async def set_client_info(self, client_info: Any) -> None:
        await self._set("client", client_info.model_dump_json())


class LoopbackOAuthCallback:
    def __init__(
        self,
        server: asyncio.AbstractServer,
        future: asyncio.Future[tuple[str, str | None]],
        *,
        interactive: bool,
    ) -> None:
        self.server = server
        self.future = future
        self.interactive = interactive
        socket = server.sockets[0]
        self.redirect_uri = f"http://127.0.0.1:{socket.getsockname()[1]}/callback"

    async def redirect(self, url: str) -> None:
        # Startup/status discovery may learn that auth is required, but only an
        # explicit login command is allowed to launch the browser.
        if not self.interactive:
            raise MCPAuthenticationRequired(url)
        opened = await asyncio.to_thread(webbrowser.open, url, 1, True)
        if not opened:
            raise RuntimeError(f"Unable to open the OAuth authorization URL: {url}")

    async def callback(self) -> tuple[str, str | None]:
        try:
            return await asyncio.wait_for(self.future, timeout=300.0)
        finally:
            await self.close()

    async def close(self) -> None:
        self.server.close()
        await self.server.wait_closed()
        if not self.future.done():
            self.future.cancel()


async def create_loopback_callback(*, interactive: bool = True) -> LoopbackOAuthCallback:
    loop = asyncio.get_running_loop()
    result: asyncio.Future[tuple[str, str | None]] = loop.create_future()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10.0)
            first_line = header.split(b"\r\n", 1)[0].decode("ascii", errors="replace")
            target = first_line.split(" ", 2)[1] if " " in first_line else "/"
            query = parse_qs(urlsplit(target).query)
            code = str((query.get("code") or [""])[0])
            state = str((query.get("state") or [""])[0]) or None
            error = str((query.get("error") or [""])[0])
            if error and not result.done():
                result.set_exception(RuntimeError(f"OAuth authorization failed: {error}"))
            elif code and not result.done():
                result.set_result((code, state))
            body = b"MiniCode authorization completed. You can close this window."
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/plain; charset=utf-8\r\n"
                + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode("ascii")
                + body
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return LoopbackOAuthCallback(server, result, interactive=interactive)


async def create_sdk_oauth_provider(
    server_url: str,
    server_name: str,
    store: TokenStore,
    *,
    interactive: bool = False,
) -> tuple[Any, LoopbackOAuthCallback]:
    from mcp.client.auth import OAuthClientProvider
    from mcp.shared.auth import OAuthClientMetadata

    callback = await create_loopback_callback(interactive=interactive)
    metadata = OAuthClientMetadata(
        redirect_uris=[callback.redirect_uri],
        token_endpoint_auth_method="none",
        client_name="MiniCode Desktop",
    )
    return OAuthClientProvider(
        server_url,
        metadata,
        store.sdk_storage(server_name),
        redirect_handler=callback.redirect,
        callback_handler=callback.callback,
    ), callback


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
