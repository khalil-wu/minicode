"""OAuth 2.1 + PKCE support for HTTP MCP servers (MCP authorization spec).

Holds MiniCode's OAuth token model and per-server token persistence plus the
SDK ``OAuthClientProvider`` assembly. The on-the-wire flow itself (PKCE,
authorization URL, token endpoint calls, refresh grants) is delegated to the
official MCP SDK; the browser + loopback-redirect interaction (env-dependent)
lives in the manager. This module stays testable without a live authorization
server.

The on-the-wire integration (Authorization header, 401 → refresh + retry) is in
``mcp.client.MCPClient``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
import asyncio
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit, urlunsplit

import keyring
from keyring.errors import KeyringError, PasswordDeleteError

from backend.atomic_io import atomic_write_text, file_mutation_locks


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


class TokenStore:
    """Per-server OAuth persistence in the OS credential store."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._service = f"minicode-mcp:{hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:20]}"

    def _load(self) -> dict[str, dict[str, Any]]:
        with file_mutation_locks([self._path]):
            return self._load_unlocked()

    def _load_unlocked(self) -> dict[str, dict[str, Any]]:
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _save(self, data: dict[str, dict[str, Any]]) -> None:
        with file_mutation_locks([self._path]):
            self._save_unlocked(data)

    def _save_unlocked(self, data: dict[str, dict[str, Any]]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Restrict the token file to the owner. These are live bearer + refresh
        # credentials; a world-readable JSON is a standing exfiltration target.
        # Pre-create a new file with 0600 so the atomic publisher preserves that
        # mode and never exposes a first-write token file under the normal 0666
        # creation mode, even briefly.
        try:
            os.chmod(self._path.parent, 0o700)
        except OSError:
            pass
        if not self._path.exists():
            try:
                fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(fd)
        atomic_write_text(
            self._path,
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        )
        try:
            os.chmod(self._path, 0o600)
        except OSError:
            pass

    def get(self, server: str) -> OAuthTokens | None:
        with file_mutation_locks([self._path]):
            try:
                secret = keyring.get_password(self._service, f"{server}:legacy")
            except KeyringError:
                secret = None
            legacy_data = self._load_unlocked() if not secret else {}
            try:
                row = json.loads(secret) if secret else legacy_data.get(server)
            except (TypeError, ValueError):
                row = None
            if not isinstance(row, dict) or not row.get("access_token"):
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
                    # Reload after the keyring write so another TokenStore's
                    # migration cannot be erased by this instance's stale map.
                    latest = self._load_unlocked()
                    latest.pop(server, None)
                    self._save_unlocked(latest)
                return tokens
            except (KeyError, TypeError, ValueError):
                return None

    def set(self, server: str, tokens: OAuthTokens) -> None:
        with file_mutation_locks([self._path]):
            payload = {
                "access_token": tokens.access_token,
                "refresh_token": tokens.refresh_token,
                "expires_at": tokens.expires_at,
                "token_type": tokens.token_type,
            }
            try:
                previous = keyring.get_password(self._service, f"{server}:legacy")
                keyring.set_password(self._service, f"{server}:legacy", json.dumps(payload))
                legacy_data = self._load_unlocked()
                if server in legacy_data:
                    legacy_data.pop(server, None)
                    self._save_unlocked(legacy_data)
            except KeyringError as exc:
                raise RuntimeError(f"OAuth token store rejected credentials: {exc}") from exc
            except Exception:
                if previous is None:
                    try:
                        keyring.delete_password(self._service, f"{server}:legacy")
                    except (KeyringError, PasswordDeleteError):
                        pass
                else:
                    try:
                        keyring.set_password(self._service, f"{server}:legacy", previous)
                    except KeyringError:
                        pass
                raise

    def clear(self, server: str) -> None:
        with file_mutation_locks([self._path]):
            for suffix in ("legacy", "tokens", "client"):
                try:
                    keyring.delete_password(self._service, f"{server}:{suffix}")
                except PasswordDeleteError:
                    pass
                except KeyringError as exc:
                    raise RuntimeError(f"OAuth token store could not clear credentials: {exc}") from exc
            legacy_data = self._load_unlocked()
            if server in legacy_data:
                legacy_data.pop(server, None)
                self._save_unlocked(legacy_data)

    def sdk_storage(self, server: str) -> "SDKTokenStorage":
        return SDKTokenStorage(self._service, server)

    def has_sdk_tokens(self, server: str) -> bool:
        try:
            raw = keyring.get_password(self._service, f"{server}:tokens")
        except KeyringError:
            return False
        if not raw:
            return False
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            return False
        return isinstance(payload, dict) and bool(str(payload.get("access_token") or "").strip())


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
        callback_path: str,
    ) -> None:
        self.server = server
        self.future = future
        self.interactive = interactive
        self.callback_path = callback_path
        socket = server.sockets[0]
        self.redirect_uri = f"http://127.0.0.1:{socket.getsockname()[1]}{callback_path}"

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


async def create_loopback_callback(
    *,
    interactive: bool = True,
    port: int | None = None,
    server_url: str = "",
) -> LoopbackOAuthCallback:
    if port is not None and not 1 <= int(port) <= 65535:
        raise ValueError(f"invalid MCP OAuth callback port {port!r}: expected 1..65535")
    callback_path = "/callback"
    if server_url:
        parsed_server_url = urlsplit(server_url)
        if parsed_server_url.scheme not in {"http", "https"} or not parsed_server_url.hostname:
            raise ValueError(f"invalid MCP server URL {server_url!r}")
        normalized_server_url = urlunsplit(
            (
                parsed_server_url.scheme,
                parsed_server_url.netloc,
                parsed_server_url.path,
                parsed_server_url.query,
                "",
            )
        )
        callback_id = base64.urlsafe_b64encode(
            hashlib.sha256(normalized_server_url.encode("utf-8")).digest()[:9]
        ).decode("ascii").rstrip("=")
        callback_path = f"/callback/{callback_id}"

    loop = asyncio.get_running_loop()
    result: asyncio.Future[tuple[str, str | None]] = loop.create_future()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10.0)
            first_line = header.split(b"\r\n", 1)[0].decode("ascii", errors="replace")
            request_parts = first_line.split(" ", 2)
            target = request_parts[1] if len(request_parts) == 3 else ""
            parsed_target = urlsplit(target)
            query = parse_qs(parsed_target.query, keep_blank_values=True)
            code = str((query.get("code") or [""])[0])
            state = str((query.get("state") or [""])[0]) or None
            error = str((query.get("error") or [""])[0])
            error_description = str((query.get("error_description") or [""])[0])
            valid_route = (
                len(request_parts) == 3
                and request_parts[0] == "GET"
                and parsed_target.path == callback_path
            )
            if not valid_route or not (error or (code and state)):
                status = b"400 Bad Request"
                body = b"Invalid OAuth callback."
            elif error:
                status = b"400 Bad Request"
                message = error_description or error
                body = b"OAuth authorization failed. You can close this window."
                if not result.done():
                    result.set_exception(RuntimeError(f"OAuth authorization failed: {message}"))
            elif not result.done():
                status = b"200 OK"
                body = b"MiniCode authorization completed. You can close this window."
                result.set_result((code, state))
            else:
                status = b"409 Conflict"
                body = b"OAuth callback has already completed."
            writer.write(
                b"HTTP/1.1 "
                + status
                + b"\r\nContent-Type: text/plain; charset=utf-8\r\n"
                + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode("ascii")
                + body
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", int(port or 0))
    return LoopbackOAuthCallback(
        server,
        result,
        interactive=interactive,
        callback_path=callback_path,
    )


async def create_sdk_oauth_provider(
    server_url: str,
    server_name: str,
    store: TokenStore,
    *,
    interactive: bool = False,
    client_id: str = "",
    callback_port: int | None = None,
) -> tuple[Any, LoopbackOAuthCallback]:
    from mcp.client.auth import OAuthClientProvider
    from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata

    callback = await create_loopback_callback(
        interactive=interactive,
        port=callback_port,
        server_url=server_url,
    )
    try:
        metadata = OAuthClientMetadata(
            redirect_uris=[callback.redirect_uri],
            token_endpoint_auth_method="none",
            client_name="MiniCode Desktop",
        )
        storage = store.sdk_storage(server_name)
        if client_id:
            await storage.set_client_info(
                OAuthClientInformationFull(
                    **metadata.model_dump(),
                    client_id=client_id,
                )
            )
        provider = OAuthClientProvider(
            server_url,
            metadata,
            storage,
            redirect_handler=callback.redirect,
            callback_handler=callback.callback,
        )
    except BaseException:
        await callback.close()
        raise
    return provider, callback
