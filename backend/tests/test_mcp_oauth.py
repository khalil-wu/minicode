"""MCP OAuth: token expiry semantics and token-store persistence.

The on-the-wire flow (PKCE, authorization URL, token endpoint, refresh
grants) is delegated to the official MCP SDK and exercised in the SDK's own
tests; only MiniCode-owned pieces are unit-tested here."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import time
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from backend.mcp.oauth import OAuthTokens, TokenStore


def test_tokens_expiry_skew():
    fresh = OAuthTokens(access_token="a", expires_at=time.time() + 60)
    assert not fresh.is_expired()
    stale = OAuthTokens(access_token="a", expires_at=time.time() - 10)
    assert stale.is_expired()
    no_expiry = OAuthTokens(access_token="a")  # expires_at=0
    assert not no_expiry.is_expired()  # 0 = no known expiry


def test_token_store_roundtrip(tmp_path: Path):
    store = TokenStore(tmp_path / "tokens.json")
    tokens = OAuthTokens(access_token="a", refresh_token="r", expires_at=1234.0, token_type="Bearer")
    store.set("srv1", tokens)

    loaded = store.get("srv1")
    assert loaded is not None
    assert loaded.access_token == "a"
    assert loaded.refresh_token == "r"
    assert loaded.expires_at == 1234.0

    store.clear("srv1")
    assert store.get("srv1") is None
    assert not (tmp_path / "tokens.json").exists()


def test_token_store_publishes_owner_only_json_atomically(tmp_path: Path):
    path = tmp_path / "tokens.json"
    # ``set`` normally puts live credentials in the OS keyring; exercise the
    # JSON fallback publisher directly so this regression covers its fsync and
    # file-mode contract as well.
    TokenStore(path)._save_unlocked({
        "srv1": {
            "access_token": "access",
            "refresh_token": "refresh",
            "expires_at": 0.0,
            "token_type": "Bearer",
        },
    })

    assert json.loads(path.read_text(encoding="utf-8"))["srv1"]["access_token"] == "access"
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_token_store_missing_server_returns_none(tmp_path: Path):
    store = TokenStore(tmp_path / "tokens.json")
    assert store.get("never") is None


def test_token_store_migrates_legacy_json_to_keyring(tmp_path: Path):
    path = tmp_path / "tokens.json"
    path.write_text(
        json.dumps({
            "legacy-server": {
                "access_token": "legacy-access",
                "refresh_token": "legacy-refresh",
                "expires_at": 123.0,
                "token_type": "Bearer",
            }
        }),
        encoding="utf-8",
    )
    store = TokenStore(path)

    loaded = store.get("legacy-server")

    assert loaded is not None
    assert loaded.access_token == "legacy-access"
    assert "legacy-server" not in json.loads(path.read_text(encoding="utf-8"))
    assert store.get("legacy-server").refresh_token == "legacy-refresh"  # type: ignore[union-attr]


def test_sdk_token_storage_roundtrip_uses_official_mcp_models(tmp_path: Path):
    from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

    storage = TokenStore(tmp_path / "tokens.json").sdk_storage("server")

    async def scenario() -> None:
        token = OAuthToken(access_token="access", token_type="bearer", refresh_token="refresh")
        client = OAuthClientInformationFull(
            client_id="client-id",
            redirect_uris=["http://127.0.0.1/callback"],
        )
        await storage.set_tokens(token)
        await storage.set_client_info(client)
        assert (await storage.get_tokens()).access_token == "access"
        assert (await storage.get_client_info()).client_id == "client-id"

    asyncio.run(scenario())


@pytest.mark.parametrize("replacement_url,replacement_client", [
    ("https://replacement.example/mcp", "client-a"),
    ("https://original.example/other", "client-a"),
    ("https://original.example/mcp", "client-b"),
])
def test_oauth_credentials_stay_with_their_endpoint_and_client(tmp_path, monkeypatch, replacement_url, replacement_client):
    from mcp.shared.auth import OAuthToken

    from backend.mcp.client import MCPClient, MCPTransport
    from backend.mcp.manager import MCPAuthStatus, MCPServerConfig, MCPServerManager, MCPServerState
    from backend.mcp.oauth import create_sdk_oauth_provider

    credentials = {}
    monkeypatch.setattr("backend.mcp.oauth.keyring.get_password", lambda service, key: credentials.get((service, key)))
    monkeypatch.setattr("backend.mcp.oauth.keyring.set_password", lambda service, key, value: credentials.__setitem__((service, key), value))
    monkeypatch.setattr("backend.mcp.oauth.keyring.delete_password", lambda service, key: credentials.pop((service, key), None))

    async def scenario():
        config_path = tmp_path / "config.json"
        store = TokenStore(config_path.with_name("mcp_tokens.json"))
        original_url = "https://original.example/mcp"
        original, callback = await create_sdk_oauth_provider(original_url, "docs", store, client_id="client-a")
        try:
            await original.context.storage.set_tokens(OAuthToken(access_token="original-token", token_type="Bearer", refresh_token="original-refresh"))
        finally:
            await callback.close()
        store.for_server(original_url, "client-a").set("docs", OAuthTokens(access_token="legacy-original-token"))

        manager = MCPServerManager(config_path=config_path, workspace_root=tmp_path)
        original_config = MCPServerConfig(name="docs", transport="http", url=original_url, oauth_client_id="client-a")
        replacement_config = MCPServerConfig(name="docs", transport="http", url=replacement_url, oauth_client_id=replacement_client)
        assert manager._stored_auth_status(original_config) == MCPAuthStatus.OAUTH
        assert manager._stored_auth_status(replacement_config) == MCPAuthStatus.NOT_LOGGED_IN
        replacement_client_instance = MCPClient("docs", transport=MCPTransport.HTTP, url=replacement_url,
                                                oauth_client_id=replacement_client, token_store=store)
        assert "Authorization" not in replacement_client_instance._http_headers()

        replacement, replacement_callback = await create_sdk_oauth_provider(replacement_url, "docs", store, client_id=replacement_client)
        flow = replacement.async_auth_flow(httpx.Request("POST", replacement_url))
        try:
            request = await anext(flow)
            assert "authorization" not in request.headers
            await replacement.context.storage.set_tokens(OAuthToken(access_token="replacement-token", token_type="Bearer"))
        finally:
            await flow.aclose()
            await replacement_callback.close()

        restored, restored_callback = await create_sdk_oauth_provider(original_url, "docs", TokenStore(store._path), client_id="client-a")
        restored_flow = restored.async_auth_flow(httpx.Request("POST", original_url))
        try:
            assert (await anext(restored_flow)).headers["authorization"] == "Bearer original-token"
        finally:
            await restored_flow.aclose()
            await restored_callback.close()

        manager._servers["docs"] = MCPServerState(config=original_config)
        monkeypatch.setattr(manager, "stop_server", AsyncMock(return_value=True))
        await manager.oauth_logout("docs")
        assert manager._stored_auth_status(original_config) == MCPAuthStatus.NOT_LOGGED_IN
        assert manager._stored_auth_status(replacement_config) == MCPAuthStatus.OAUTH

    asyncio.run(scenario())


def test_name_only_legacy_tokens_are_not_rebound_to_the_current_url(tmp_path, monkeypatch):
    from mcp.shared.auth import OAuthToken

    from backend.mcp.client import MCPClient, MCPTransport
    from backend.mcp.oauth import create_sdk_oauth_provider

    credentials = {}
    monkeypatch.setattr("backend.mcp.oauth.keyring.get_password", lambda service, key: credentials.get((service, key)))
    monkeypatch.setattr("backend.mcp.oauth.keyring.set_password", lambda service, key, value: credentials.__setitem__((service, key), value))

    async def scenario():
        store = TokenStore(tmp_path / "tokens.json")
        store.set("docs", OAuthTokens(access_token="unbound-legacy-token"))
        await store.sdk_storage("docs").set_tokens(OAuthToken(access_token="unbound-sdk-token", token_type="Bearer"))
        url = "https://current.example/mcp"
        client = MCPClient("docs", transport=MCPTransport.HTTP, url=url, token_store=store)
        assert "Authorization" not in client._http_headers()
        provider, callback = await create_sdk_oauth_provider(url, "docs", store)
        flow = provider.async_auth_flow(httpx.Request("POST", url))
        try:
            assert "authorization" not in (await anext(flow)).headers
        finally:
            await flow.aclose()
            await callback.close()

    asyncio.run(scenario())
