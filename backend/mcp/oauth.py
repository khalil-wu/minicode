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

from backend.async_cleanup import to_thread_cancel_safe

import base64
import hashlib
import json
import math
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
from filelock import AsyncFileLock, FileLock
from mcp.client.auth import OAuthClientProvider as SDKOAuthClientProvider

from backend.atomic_io import atomic_write_text, file_mutation_locks

_CREDENTIAL_ERRORS = (KeyringError,)
if os.name == "nt":
    import pywintypes

    _CREDENTIAL_ERRORS += (pywintypes.error,)


class MCPTokenStoreError(RuntimeError):
    """An existing MCP credential or its legacy file cannot be read or committed."""


class MCPAuthenticationRequired(ConnectionError):
    """A remote MCP server requires an explicit user-initiated OAuth login."""

    mcp_auth_required = True
    mcp_auth_expired = False

    def __init__(self, authorization_url: str = "") -> None:
        super().__init__("Authentication required; sign in from the Connectors settings.")
        self.authorization_url = authorization_url


def _credential_lock_path(service: str, server: str, path: Path) -> Path:
    identity = hashlib.sha256(f"{service}:{server}".encode()).hexdigest()
    return path.parent / f".mcp-oauth-{identity}.lock"


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

    def for_server(self, server_url: str, client_id: str = "") -> "TokenStore":
        endpoint = urlunsplit(urlsplit(server_url.strip())._replace(fragment=""))
        identity = json.dumps([endpoint, client_id.strip()], separators=(",", ":"))
        scope = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
        return TokenStore(self._path.with_name(f"{self._path.stem}-{scope}{self._path.suffix}"))

    def _load(self) -> dict[str, dict[str, Any]]:
        with file_mutation_locks([self._path]):
            return self._load_unlocked()

    def _load_unlocked(self) -> dict[str, dict[str, Any]]:
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            raise MCPTokenStoreError("MCP legacy credential file cannot be read") from exc
        if not isinstance(payload, dict):
            raise MCPTokenStoreError("MCP legacy credential file must contain an object")
        return payload

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
            except _CREDENTIAL_ERRORS as exc:
                raise MCPTokenStoreError("MCP credential store cannot read legacy credentials") from exc
            legacy_data = self._load_unlocked() if secret is None else {}
            try:
                row = json.loads(secret) if secret is not None else legacy_data.get(server)
                if row is None and secret is None:
                    return None
                if not isinstance(row, dict) or not isinstance(row.get("access_token"), str) or not row["access_token"].strip():
                    raise ValueError("stored access token must be a non-empty string")
                expires_at = float(row.get("expires_at", 0.0) or 0.0)
                if not math.isfinite(expires_at):
                    raise ValueError("stored expiry must be finite")
                tokens = OAuthTokens(
                    access_token=row["access_token"],
                    refresh_token=str(row.get("refresh_token") or ""),
                    expires_at=expires_at,
                    token_type=str(row.get("token_type", "Bearer") or "Bearer"),
                )
            except (KeyError, TypeError, ValueError, OverflowError) as exc:
                raise MCPTokenStoreError("MCP legacy credential record is invalid") from exc
            if secret is None:
                self.set(server, tokens)
            return tokens

    def _commit_credentials(self, server: str, changes: dict[str, str | None]) -> None:
        with file_mutation_locks([self._path]):
            legacy_data = self._load_unlocked()
            original_file = self._path.read_bytes() if self._path.exists() else None
            try:
                previous = {suffix: keyring.get_password(self._service, f"{server}:{suffix}") for suffix in changes}
            except _CREDENTIAL_ERRORS as exc:
                raise MCPTokenStoreError("MCP credential store cannot read the previous credential family") from exc
            try:
                for suffix, value in changes.items():
                    if value is None:
                        try:
                            keyring.delete_password(self._service, f"{server}:{suffix}")
                        except PasswordDeleteError:
                            pass
                    else:
                        keyring.set_password(self._service, f"{server}:{suffix}", value)
                if server in legacy_data:
                    legacy_data.pop(server)
                    self._save_unlocked(legacy_data)
            except Exception as failure:
                rollback_errors = []
                for suffix, value in previous.items():
                    try:
                        if value is None:
                            try:
                                keyring.delete_password(self._service, f"{server}:{suffix}")
                            except PasswordDeleteError:
                                pass
                        else:
                            keyring.set_password(self._service, f"{server}:{suffix}", value)
                    except _CREDENTIAL_ERRORS as exc:
                        rollback_errors.append(exc)
                try:
                    current_file = self._path.read_bytes() if self._path.exists() else None
                    if current_file != original_file:
                        if original_file is None:
                            self._path.unlink()
                        else:
                            atomic_write_text(self._path, original_file.decode("utf-8"))
                except OSError as exc:
                    rollback_errors.append(exc)
                if rollback_errors:
                    raise ExceptionGroup("MCP credential publication and rollback failed", [failure, *rollback_errors]) from failure
                if isinstance(failure, _CREDENTIAL_ERRORS):
                    raise MCPTokenStoreError("MCP credential store rejected the credential family") from failure
                raise

    def set(self, server: str, tokens: OAuthTokens) -> None:
        payload = {
            "access_token": tokens.access_token,
            "refresh_token": tokens.refresh_token,
            "expires_at": tokens.expires_at,
            "token_type": tokens.token_type,
        }
        self._commit_credentials(server, {"legacy": json.dumps(payload)})

    def clear(self, server: str) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(_credential_lock_path(self._service, server, self._path)), timeout=60):
            self._commit_credentials(server, dict.fromkeys(("legacy", "tokens", "client")))

    def sdk_storage(self, server: str) -> "SDKTokenStorage":
        return SDKTokenStorage(self._service, server, self._path)

    def has_sdk_tokens(self, server: str) -> bool:
        try:
            raw = keyring.get_password(self._service, f"{server}:tokens")
        except _CREDENTIAL_ERRORS as exc:
            raise MCPTokenStoreError("MCP credential store cannot read SDK tokens") from exc
        if raw is None:
            return False
        SDKTokenStorage._parse_tokens(raw)
        return True


class SDKTokenStorage:
    """Adapter for the official MCP SDK TokenStorage protocol."""

    def __init__(self, service: str, server: str, path: Path) -> None:
        self._service = service
        self._server = server
        self.refresh_lock_path = _credential_lock_path(service, server, path)
        self.refresh_lock = AsyncFileLock(str(self.refresh_lock_path), timeout=60)
        self.expires_at: float | None = None
        self.oauth_metadata: Any = None
        self.protected_resource_metadata: Any = None
        self.auth_server_url: str | None = None
        self.context: Any = None

    async def _get(self, suffix: str) -> str | None:
        try:
            return await asyncio.to_thread(
                keyring.get_password, self._service, f"{self._server}:{suffix}",
            )
        except _CREDENTIAL_ERRORS as exc:
            raise MCPTokenStoreError("MCP credential store cannot read SDK credentials") from exc

    async def _set(self, suffix: str, value: str) -> None:
        write = asyncio.create_task(to_thread_cancel_safe(
            keyring.set_password,
            self._service,
            f"{self._server}:{suffix}",
            value,
        ))
        cancelled = False
        # Native credential writes cannot be cancelled. Finish this one write
        # before releasing its refresh lock, including on repeated cancellation.
        while not write.done():
            try:
                await asyncio.shield(write)
            except asyncio.CancelledError:
                cancelled = True
        write.result()
        if cancelled:
            raise asyncio.CancelledError

    @staticmethod
    def _parse_tokens(raw: str) -> tuple[Any, float | None, Any, Any, str | None]:
        from mcp.shared.auth import OAuthMetadata, OAuthToken, ProtectedResourceMetadata

        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("stored SDK token must be an object")
            # Old records lack acquisition time; preserve the one-time refresh.
            expiry = payload.pop("_minicode_expires_at", 1.0 if payload.get("expires_in") is not None else None)
            expires_at = float(expiry) if expiry is not None else None
            if expires_at is not None and not math.isfinite(expires_at):
                raise ValueError("stored expiry must be finite")
            metadata = payload.pop("_minicode_oauth_metadata", None)
            resource = payload.pop("_minicode_resource_metadata", None)
            auth_server_url = payload.pop("_minicode_auth_server_url", None)
            if auth_server_url is not None and not isinstance(auth_server_url, str):
                raise ValueError("stored auth server URL must be a string")
            oauth_metadata = OAuthMetadata.model_validate(metadata) if metadata is not None else None
            protected_resource_metadata = ProtectedResourceMetadata.model_validate(resource) if resource is not None else None
            tokens = OAuthToken.model_validate(payload)
            if not tokens.access_token.strip():
                raise ValueError("stored access token must not be empty")
        except (TypeError, ValueError, OverflowError) as exc:
            raise MCPTokenStoreError("MCP SDK token record is invalid") from exc
        return tokens, expires_at, oauth_metadata, protected_resource_metadata, auth_server_url

    async def get_tokens(self) -> Any | None:
        raw = await self._get("tokens")
        if raw is None:
            self.expires_at = None
            return None
        tokens, expires_at, oauth_metadata, protected_resource_metadata, auth_server_url = self._parse_tokens(raw)
        self.expires_at = expires_at
        self.oauth_metadata = oauth_metadata
        self.protected_resource_metadata = protected_resource_metadata
        self.auth_server_url = auth_server_url
        return tokens

    async def set_tokens(self, tokens: Any) -> None:
        payload = tokens.model_dump(mode="json")
        expires_at = time.time() + tokens.expires_in if tokens.expires_in is not None else None
        payload["_minicode_expires_at"] = expires_at
        if self.context is not None:
            metadata = self.context.oauth_metadata
            resource = self.context.protected_resource_metadata
            payload["_minicode_oauth_metadata"] = metadata.model_dump(mode="json") if metadata is not None else None
            payload["_minicode_resource_metadata"] = resource.model_dump(mode="json") if resource is not None else None
            payload["_minicode_auth_server_url"] = self.context.auth_server_url
        await self._set("tokens", json.dumps(payload))
        self.expires_at = expires_at

    async def get_client_info(self) -> Any | None:
        from mcp.shared.auth import OAuthClientInformationFull

        raw = await self._get("client")
        if raw is None:
            return None
        try:
            return OAuthClientInformationFull.model_validate_json(raw)
        except (TypeError, ValueError) as exc:
            raise MCPTokenStoreError("MCP SDK client record is invalid") from exc

    async def set_client_info(self, client_info: Any) -> None:
        await self._set("client", client_info.model_dump_json())


class CredentialOAuthProvider(SDKOAuthClientProvider):
    """Keep the SDK flow inside its shared credential's refresh transaction."""

    def __init__(self, *args: Any, configured_client_info: Any = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._configured_client_info = configured_client_info
        self.context.storage.context = self.context

    async def _initialize(self) -> None:
        await super()._initialize()
        storage = self.context.storage
        self.context.token_expiry_time = storage.expires_at
        self.context.oauth_metadata = storage.oauth_metadata
        self.context.protected_resource_metadata = storage.protected_resource_metadata
        self.context.auth_server_url = storage.auth_server_url
        if self._configured_client_info is not None:
            if self.context.client_info != self._configured_client_info:
                await storage.set_client_info(self._configured_client_info)
            self.context.client_info = self._configured_client_info

    async def async_auth_flow(self, request: Any):
        storage = self.context.storage
        storage.refresh_lock_path.parent.mkdir(parents=True, exist_ok=True)
        async with storage.refresh_lock:
            # Re-read inside the lock: another manager/process may have rotated
            # these tokens while this provider retained its previous context.
            self._initialized = False
            flow = super().async_auth_flow(request)
            try:
                outgoing = await anext(flow)
                while True:
                    response = yield outgoing
                    outgoing = await flow.asend(response)
            except StopAsyncIteration:
                return
            finally:
                await flow.aclose()


class LoopbackOAuthCallback:
    def __init__(
        self,
        server: asyncio.AbstractServer,
        future: asyncio.Future[tuple[str, str | None]],
        *,
        interactive: bool,
        callback_path: str,
        handler_tasks: set[asyncio.Task[None]],
    ) -> None:
        self.server = server
        self.future = future
        self.interactive = interactive
        self.callback_path = callback_path
        self._handler_tasks = handler_tasks
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
        # Accepted transports queue connection_made with call_soon. Dispatch
        # those callbacks before taking ownership of their handler tasks.
        await asyncio.sleep(0)
        await self.server.wait_closed()
        # Server.wait_closed only closes the listener on Python 3.11. Own the
        # accepted callbacks explicitly until their stream/finally has settled.
        peers = set(self._handler_tasks)
        if peers:
            await asyncio.gather(*peers)
        if not self.future.done():
            self.future.cancel()

    @property
    def active_connections(self) -> int:
        return len(self._handler_tasks)


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
    handler_tasks: set[asyncio.Task[None]] = set()

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

    def own_connection(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.create_task(handle(reader, writer))
        handler_tasks.add(task)
        task.add_done_callback(handler_tasks.discard)

    server = await asyncio.start_server(own_connection, "127.0.0.1", int(port or 0))
    return LoopbackOAuthCallback(
        server,
        result,
        interactive=interactive,
        callback_path=callback_path,
        handler_tasks=handler_tasks,
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
        # Name-only credentials cannot establish which endpoint owns them.
        storage = store.for_server(server_url, client_id).sdk_storage(server_name)
        configured_client_info = (
            OAuthClientInformationFull(**metadata.model_dump(), client_id=client_id)
            if client_id else None
        )
        provider = CredentialOAuthProvider(
            server_url,
            metadata,
            storage,
            redirect_handler=callback.redirect,
            callback_handler=callback.callback,
            configured_client_info=configured_client_info,
        )
    except BaseException:
        await callback.close()
        raise
    return provider, callback
