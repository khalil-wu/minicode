from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.vault.store import EnvVault, VaultReadError


def test_corrupt_index_refuses_mutations_instead_of_wiping(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault.json"
    vault = EnvVault(vault_path)
    vault_path.write_text("{ not json", encoding="utf-8")

    with pytest.raises(RuntimeError):
        vault.set("new-secret", "value")
    with pytest.raises(RuntimeError):
        vault.delete("anything")
    with pytest.raises(VaultReadError):
        vault.list_names()
    with pytest.raises(VaultReadError):
        vault.get("anything")

    # The corrupt file is left untouched for manual repair.
    assert vault_path.read_text(encoding="utf-8") == "{ not json"


def test_healthy_round_trip(tmp_path: Path, monkeypatch) -> None:
    import backend.vault.store as store

    vault_path = tmp_path / "vault.json"
    vault = EnvVault(vault_path)
    monkeypatch.setattr(store.keyring, "set_password", lambda *_a, **_k: None)
    monkeypatch.setattr(store.keyring, "get_password", lambda *_a, **_k: "value")
    monkeypatch.setattr(store.keyring, "delete_password", lambda *_a, **_k: None)

    vault.set("secret-name", "value", description="d")
    payload = json.loads(vault_path.read_text(encoding="utf-8"))
    assert payload["entries"]["secret-name"]["description"] == "d"
    assert vault.delete("secret-name") is True
    assert vault.delete("missing") is False


@pytest.fixture
def credential_store(monkeypatch):
    import backend.vault.store as store

    values = {}
    monkeypatch.setattr(store.keyring, "get_password", lambda service, name: values.get((service, name)))
    monkeypatch.setattr(store.keyring, "set_password", lambda service, name, value: values.__setitem__((service, name), value))
    monkeypatch.setattr(store.keyring, "delete_password", lambda service, name: values.pop((service, name), None))
    return values


def test_missing_os_credential_is_metadata_and_does_not_block_unrelated_injection(tmp_path, credential_store):
    from backend.services.env_vault_service import list_env_entries

    vault = EnvVault(tmp_path / "vault.json")
    vault.set("LOST_GLOBAL", "removed-value", description="Search service")
    vault.set("VALID_GLOBAL", "kept-value")
    vault.set("SHELL_ONLY", "shell-value", scope="run_command")
    vault.set("MCP_ONLY", "mcp-value", scope="mcp:search")
    credential_store.pop((vault._service, "LOST_GLOBAL"))
    original_index = vault._path.read_bytes()

    assert vault.get("LOST_GLOBAL") is None
    assert vault.inject_into_env("run_command") == {"VALID_GLOBAL": "kept-value", "SHELL_ONLY": "shell-value"}
    assert vault.inject_into_env("mcp:search") == {"VALID_GLOBAL": "kept-value", "MCP_ONLY": "mcp-value"}
    entries = list_env_entries(vault).entries
    assert entries[0] == {"name": "LOST_GLOBAL", "description": "Search service", "scope": "global", "credential_status": "missing"}
    assert all(entry["credential_status"] == "stored" for entry in entries[1:])
    assert "kept-value" not in json.dumps(entries)
    assert vault._path.read_bytes() == original_index


def test_missing_credential_can_be_resaved_with_its_scope_or_removed(tmp_path, credential_store):
    from backend.services.env_vault_service import delete_env_entry, set_env_entry

    vault = EnvVault(tmp_path / "vault.json")
    vault.set("SEARCH_KEY", "removed-value", description="Search", scope="mcp:search")
    credential_store.pop((vault._service, "SEARCH_KEY"))
    result = set_env_entry({"name": "SEARCH_KEY", "value": "replacement-value", "description": "Search", "scope": "mcp:search"}, vault)
    assert result.entries == [{"name": "SEARCH_KEY", "description": "Search", "scope": "mcp:search", "credential_status": "stored"}]
    assert vault.inject_into_env("mcp:search") == {"SEARCH_KEY": "replacement-value"}
    assert vault.inject_into_env("run_command") == {}
    assert delete_env_entry({"name": "SEARCH_KEY"}, vault).entries == []
    assert "SEARCH_KEY" not in json.loads(vault._path.read_text())["entries"]


def test_os_read_failure_remains_an_error_for_reads_metadata_and_injection(tmp_path, credential_store, monkeypatch):
    import backend.vault.store as store
    from keyring.errors import KeyringError

    vault = EnvVault(tmp_path / "vault.json")
    vault.set("SECRET", "stored-value")
    original_index = vault._path.read_bytes()
    def unreadable(_service, _name):
        raise KeyringError("credential store unavailable")
    monkeypatch.setattr(store.keyring, "get_password", unreadable)
    for read in (lambda: vault.get("SECRET"), vault.list_names, vault.inject_into_env):
        with pytest.raises(VaultReadError, match="could not read vault entry SECRET"):
            read()
    assert vault._path.read_bytes() == original_index


@pytest.mark.parametrize("entry", [
    {"value": "AAAA"},
    {"salt": "AAAA"},
    {"value": "not*base64", "salt": "AAAA"},
])
def test_corrupt_legacy_credential_remains_an_error_and_is_not_erased(tmp_path, credential_store, entry):
    vault_path = tmp_path / "vault.json"
    vault_path.write_text(json.dumps({"version": 1, "entries": {"LEGACY": entry}}), encoding="utf-8")
    original_index = vault_path.read_bytes()
    vault = EnvVault(vault_path)
    with pytest.raises(VaultReadError):
        vault.get("LEGACY")
    with pytest.raises(VaultReadError):
        vault.list_names()
    assert vault_path.read_bytes() == original_index
