from __future__ import annotations

import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from keyring.errors import KeyringError, PasswordDeleteError

from backend.config_helpers import _coerce_model_metadata, get_provider_model_metadata
from backend.llm.provider_contracts import MAX_SAFE_INTEGER
from backend.mcp.manager import MCPServerManager, MCPServerConfig, MCPServerState, ServerStatus, MCPAuthStatus
from backend.mcp.oauth import MCPTokenStoreError, OAuthTokens, TokenStore
from backend.services.llm_provider_helpers import _extract_model_discovery
from backend.services.mcp_service import logout_mcp_server


@pytest.fixture
def credentials(monkeypatch):
    values = {}
    monkeypatch.setattr("keyring.get_password", lambda service, name: values.get((service, name)))
    monkeypatch.setattr("keyring.set_password", lambda service, name, value: values.__setitem__((service, name), value))

    def delete(service, name):
        if (service, name) not in values:
            raise PasswordDeleteError("absent")
        del values[service, name]

    monkeypatch.setattr("keyring.delete_password", delete)
    return values


@pytest.mark.parametrize("suffix", ["legacy", "tokens", "client"])
def test_clear_failure_restores_all_records_and_exact_legacy_file(monkeypatch, tmp_path, credentials, suffix):
    store = TokenStore(tmp_path / "legacy.json")
    store._save_unlocked({"fixture": {"access_token": "legacy-placeholder"}, "other": {"access_token": "other-placeholder"}})
    credentials.update({(store._service, "fixture:" + key): "placeholder-" + key for key in ("legacy", "tokens", "client")})
    before = dict(credentials), store._path.read_bytes()

    def unavailable(service, name):
        if name == "fixture:" + suffix:
            raise KeyringError("injected deletion failure")
        credentials.pop((service, name), None)

    monkeypatch.setattr("keyring.delete_password", unavailable)
    with pytest.raises(MCPTokenStoreError):
        store.clear("fixture")
    assert credentials == before[0]
    assert store._path.read_bytes() == before[1]


def test_write_then_failure_restores_exact_previous_os_record(monkeypatch, tmp_path, credentials):
    store = TokenStore(tmp_path / "legacy.json")
    credentials[store._service, "fixture:legacy"] = "old-placeholder"
    writes = []

    def write(service, name, value):
        credentials[service, name] = value
        writes.append(value)
        if len(writes) == 1:
            raise KeyringError("injected backend failure after write")

    monkeypatch.setattr("keyring.set_password", write)
    with pytest.raises(MCPTokenStoreError):
        store.set("fixture", OAuthTokens(access_token="new-placeholder"))
    assert credentials[store._service, "fixture:legacy"] == "old-placeholder"
    assert not store._path.exists()


def test_file_publication_failure_restores_credentials_and_file(monkeypatch, tmp_path, credentials):
    store = TokenStore(tmp_path / "legacy.json")
    store._save_unlocked({"fixture": {"access_token": "old-placeholder"}})
    credentials[store._service, "fixture:legacy"] = "old-raw-placeholder"
    before = dict(credentials), store._path.read_bytes()
    original = store._save_unlocked

    def publish(data):
        original(data)
        raise OSError("injected legacy cleanup failure after publication")

    monkeypatch.setattr(store, "_save_unlocked", publish)
    with pytest.raises(OSError):
        store.set("fixture", OAuthTokens(access_token="new-placeholder"))
    assert credentials == before[0]
    assert store._path.read_bytes() == before[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("consumer", ["legacy", "has_sdk", "sdk_tokens", "sdk_client"])
async def test_native_store_read_failure_never_returns_missing(monkeypatch, tmp_path, consumer):
    store = TokenStore(tmp_path / "legacy.json")

    def unreadable(*args, **kwargs):
        if os.name == "nt":
            import pywintypes
            raise pywintypes.error(5, "CredRead", "injected OS denial")
        raise KeyringError("injected OS denial")

    monkeypatch.setattr("keyring.get_password", unreadable)
    with pytest.raises(MCPTokenStoreError):
        if consumer == "legacy":
            store.get("fixture")
        elif consumer == "has_sdk":
            store.has_sdk_tokens("fixture")
        elif consumer == "sdk_tokens":
            await store.sdk_storage("fixture").get_tokens()
        else:
            await store.sdk_storage("fixture").get_client_info()


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ["", "[]", "{}", '{"access_token":false}', '{"access_token":"placeholder","token_type":"Bearer","_minicode_expires_at":NaN,"expires_at":NaN}'])
async def test_corrupt_stored_records_are_errors_not_absence(tmp_path, credentials, raw):
    store = TokenStore(tmp_path / "legacy.json")
    credentials[store._service, "fixture:legacy"] = raw
    credentials[store._service, "fixture:tokens"] = raw
    with pytest.raises(MCPTokenStoreError):
        store.get("fixture")
    with pytest.raises(MCPTokenStoreError):
        store.has_sdk_tokens("fixture")
    with pytest.raises(MCPTokenStoreError):
        await store.sdk_storage("fixture").get_tokens()


def test_corrupt_legacy_file_refuses_clear_without_touching_os(tmp_path, credentials):
    store = TokenStore(tmp_path / "legacy.json")
    store._path.write_text("{broken", encoding="utf-8")
    credentials[store._service, "fixture:tokens"] = "placeholder"
    before = dict(credentials)
    with pytest.raises(MCPTokenStoreError):
        store.get("fixture")
    with pytest.raises(MCPTokenStoreError):
        store.clear("fixture")
    assert credentials == before
    assert store._path.read_text(encoding="utf-8") == "{broken"


@pytest.mark.asyncio
async def test_failed_sdk_write_does_not_publish_new_expiry(monkeypatch, tmp_path, credentials):
    from mcp.shared.auth import OAuthToken

    storage = TokenStore(tmp_path / "legacy.json").sdk_storage("fixture")
    storage.expires_at = 123.0

    def unavailable(*args, **kwargs):
        raise KeyringError("injected write denial")

    monkeypatch.setattr("keyring.set_password", unavailable)
    with pytest.raises(KeyringError):
        await storage.set_tokens(OAuthToken(access_token="placeholder", token_type="Bearer", expires_in=60))
    assert storage.expires_at == 123.0


@pytest.mark.asyncio
async def test_optional_storage_failure_is_owned_and_other_server_connects(monkeypatch, tmp_path, credentials):
    manager = MCPServerManager(config_path=tmp_path / "config.json")
    bad = MCPServerConfig(name="unreadable", transport="http", url="https://unreadable.invalid/mcp")
    healthy = MCPServerConfig(name="healthy", transport="http", url="https://healthy.invalid/mcp")
    bad_service = manager._token_store.for_server(bad.url)._service
    monkeypatch.setattr(manager, "load_config", lambda: [bad, healthy])

    def read(service, name):
        if service == bad_service:
            raise KeyringError("injected one-server OS denial")
        return credentials.get((service, name))

    monkeypatch.setattr("keyring.get_password", read)
    healthy_client = SimpleNamespace(connect=AsyncMock(), close=AsyncMock(return_value=True), list_tools=AsyncMock(return_value=[]), connected=True, has_valid_token=False)
    monkeypatch.setattr(manager, "_create_client", lambda *args, **kwargs: healthy_client)
    await manager.start_all()
    state = manager._servers[bad.name]
    assert state.status is ServerStatus.ERROR
    assert isinstance(state.last_exception, MCPTokenStoreError)
    assert "credential_store" in state.operation_failures
    assert manager._servers[healthy.name].status is ServerStatus.CONNECTED
    monkeypatch.setattr("keyring.get_password", lambda service, name: credentials.get((service, name)))
    await manager.start_server(bad)
    assert state.status is ServerStatus.CONNECTED
    assert "credential_store" not in state.operation_failures


@pytest.mark.asyncio
async def test_client_constructor_store_failure_stays_in_attempt_owner(monkeypatch, tmp_path, credentials):
    manager = MCPServerManager(config_path=tmp_path / "config.json")
    config = MCPServerConfig(name="fixture", transport="http", url="https://fixture.invalid/mcp")
    state = MCPServerState(config=config)
    manager._servers[config.name] = state

    def unavailable(*args, **kwargs):
        raise MCPTokenStoreError("injected constructor storage failure")

    monkeypatch.setattr(manager, "_create_client", unavailable)
    await manager._attempt_connection(config.name, state)
    assert state.status is ServerStatus.ERROR
    assert isinstance(state.last_exception, MCPTokenStoreError)
    assert state.client is None and state.tools == []
    assert "connect" in state.operation_failures


@pytest.mark.asyncio
async def test_failed_logout_keeps_credentials_and_subscription_intent(monkeypatch, tmp_path, credentials):
    manager = MCPServerManager(config_path=tmp_path / "config.json")
    config = MCPServerConfig(name="fixture", transport="http", url="https://fixture.invalid/mcp", oauth_client_id="client")
    await manager.register_config(config)
    state = manager._servers[config.name]
    state.auth_status = MCPAuthStatus.OAUTH
    manager._resource_subscriptions[config.name] = {"resource://fixture"}
    store = manager._token_store.for_server(config.url, config.oauth_client_id)
    credentials.update({(store._service, "fixture:" + key): "placeholder" for key in ("legacy", "tokens", "client")})
    before = dict(credentials)

    def delete(service, name):
        if name == "fixture:client":
            raise KeyringError("injected logout deletion failure")
        credentials.pop((service, name), None)

    monkeypatch.setattr("keyring.delete_password", delete)
    with pytest.raises(MCPTokenStoreError):
        await logout_mcp_server(manager, config.name)
    assert credentials == before
    assert state.auth_status is MCPAuthStatus.OAUTH
    assert manager._resource_subscriptions[config.name] == {"resource://fixture"}


@pytest.mark.parametrize("value", [True, 1.5, float("inf"), float("nan"), -1, MAX_SAFE_INTEGER + 1, "9" * 5000])
def test_invalid_live_and_saved_limits_never_gain_provider_authority(value):
    discovered = _extract_model_discovery({"data": [{"id": "fixture", "context_window": value, "max_context_window": value, "max_output_tokens": value}]})
    saved = _coerce_model_metadata({"fixture": {"context_window": value, "max_context_window": value, "max_output_tokens": value}})
    assert discovered == ["fixture"]
    assert discovered.model_metadata == {}
    assert saved == {}
    projected = get_provider_model_metadata({"model": "fixture", "model_metadata": saved}, "fixture")
    assert projected["context_window_verified"] is False


@pytest.mark.parametrize("value", [128000, 128000.0, "128000", "00000128000", MAX_SAFE_INTEGER])
def test_exact_positive_limits_keep_the_same_live_and_saved_values(value):
    raw = {"context_window": value, "max_context_window": value, "max_output_tokens": value}
    discovered = _extract_model_discovery({"data": [{"id": "fixture", **raw}]})
    saved = _coerce_model_metadata({"fixture": raw})
    assert discovered.model_metadata["fixture"] == saved["fixture"]
    assert saved["fixture"]["context_window"] == int(value)
