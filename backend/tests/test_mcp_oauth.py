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
