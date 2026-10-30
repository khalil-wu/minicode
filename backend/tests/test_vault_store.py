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


def test_vault_handle_defers_reads_until_operation_and_sees_other_instance_updates(tmp_path, credential_store, monkeypatch):
    vault_path = tmp_path / "vault.json"
    writer = EnvVault(vault_path)
    writer.set("SECRET", "first")
    read_text = Path.read_text
    reads = []

    def read(path, *args, **kwargs):
        if path == vault_path:
            reads.append(path)
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    reader = EnvVault(vault_path)
    assert reads == []
    assert reader.get("SECRET") == "first"
    assert reads == [vault_path]
    writer.set("SECRET", "second")
    reads.clear()
    assert reader.get("SECRET") == "second"
    assert reads == [vault_path]
    writer.delete("SECRET")
    assert reader.get("SECRET") is None
    vault_path.write_text("{ corrupt", encoding="utf-8")
    with pytest.raises(VaultReadError):
        reader.get("SECRET")


@pytest.mark.parametrize("version", [1, 2])
def test_prefix_listing_reloads_index_checks_matching_credentials_and_skips_unrelated_values(tmp_path, monkeypatch, version):
    import backend.vault.store as store
    from keyring.errors import KeyringError

    vault_path = tmp_path / "vault.json"
    entries = {"UNRELATED": {"value": "not*base64", "salt": "AAAA"}} if version == 1 else {"UNRELATED": {}}
    vault_path.write_text(json.dumps({"version": version, "entries": entries}), encoding="utf-8")

    reads = []

    def missing(service, name):
        reads.append(name)
        return None

    def unreadable(*args):
        raise KeyringError("synthetic credential reader unavailable")

    monkeypatch.setattr(store.keyring, "get_password", missing)
    monkeypatch.setattr(store.keyring, "set_password", lambda *args: pytest.fail("name lookup migrated a credential"))
    vault = EnvVault(vault_path)
    assert vault.list_names(prefix="CUSTOM_API_KEY_") == []
    assert reads == []
    entries["CUSTOM_API_KEY_SCOPED"] = {}
    vault_path.write_text(json.dumps({"version": version, "entries": entries}), encoding="utf-8")
    original_index = vault_path.read_bytes()
    assert vault.list_names(prefix="CUSTOM_API_KEY_") == [{"name": "CUSTOM_API_KEY_SCOPED", "description": "", "scope": "global", "credential_status": "missing"}]
    assert reads == ["CUSTOM_API_KEY_SCOPED"]
    assert vault_path.read_bytes() == original_index
    monkeypatch.setattr(store.keyring, "get_password", unreadable)
    with pytest.raises(VaultReadError):
        vault.get("CUSTOM_API_KEY_SCOPED")
    with pytest.raises(VaultReadError):
        vault.list_names(prefix="CUSTOM_API_KEY_")
    monkeypatch.setattr(store.keyring, "get_password", missing)
    del entries["CUSTOM_API_KEY_SCOPED"]
    vault_path.write_text(json.dumps({"version": version, "entries": entries}), encoding="utf-8")
    assert vault.list_names(prefix="CUSTOM_API_KEY_") == []
    vault_path.write_text("{ corrupt", encoding="utf-8")
    with pytest.raises(VaultReadError):
        vault.list_names(prefix="CUSTOM_API_KEY_")


@pytest.mark.parametrize("stale_scoped_marker", [False, True])
def test_global_legacy_fallback_respects_indexed_scope_marker_with_missing_keyring_value(tmp_path, credential_store, monkeypatch, stale_scoped_marker):
    import backend.config_helpers as helpers
    import backend.vault.store as store

    vault_path = tmp_path / "vault.json"
    entries = {"CUSTOM_API_KEY": {}, "UNRELATED": {}}
    if stale_scoped_marker:
        entries["CUSTOM_API_KEY_OTHER"] = {}
    vault_path.write_text(json.dumps({"version": 2, "entries": entries}), encoding="utf-8")
    vault = EnvVault(vault_path)
    credential_store[(vault._service, "CUSTOM_API_KEY")] = "legacy-global"
    reads = []

    def read(service, name):
        assert name != "UNRELATED"
        reads.append(name)
        return credential_store.get((service, name))

    monkeypatch.setattr(store.keyring, "get_password", read)
    monkeypatch.setattr("backend.vault.EnvVault", lambda: vault)
    monkeypatch.setattr(helpers, "_RUNTIME_API_KEY_SCOPES", {})
    monkeypatch.delenv("CUSTOM_BASE_URL", raising=False)
    monkeypatch.delenv("CUSTOM_API_KEY", raising=False)
    assert helpers._custom_provider_api_key("https://requested.invalid/v1") == ("" if stale_scoped_marker else "legacy-global")
    assert reads == (["CUSTOM_API_KEY_OTHER"] if stale_scoped_marker else ["CUSTOM_API_KEY"])


def test_execution_injection_reads_one_index_and_only_current_scope_values(tmp_path, credential_store, monkeypatch):
    import backend.vault.store as store

    vault_path = tmp_path / "vault.json"
    vault = EnvVault(vault_path)
    vault.set("GLOBAL", "global")
    vault.set("SHELL", "shell", scope="run_command")
    vault.set("MCP", "mcp", scope="mcp:other")
    vault.set("CUSTOM_API_KEY_SCOPED", "provider")
    vault.set("MISSING", "removed")
    credential_store.pop((vault._service, "MISSING"))
    read_text = Path.read_text
    reads, names = [], []

    def read(path, *args, **kwargs):
        if path == vault_path:
            reads.append(path)
        return read_text(path, *args, **kwargs)

    def credential(service, name):
        names.append(name)
        return credential_store.get((service, name))

    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(store.keyring, "get_password", credential)
    assert vault.inject_into_env("run_command") == {"GLOBAL": "global", "SHELL": "shell"}
    assert reads == [vault_path]
    assert names == ["GLOBAL", "SHELL", "MISSING"]
    writer = EnvVault(vault_path)
    writer.set("GLOBAL", "updated")
    writer.set("SHELL", "moved", scope="mcp:other")
    reads.clear()
    names.clear()
    assert vault.inject_into_env("run_command") == {"GLOBAL": "updated"}
    assert reads == [vault_path]
    assert names == ["GLOBAL", "MISSING"]


def test_execution_injection_migrates_selected_v1_entries_without_erasing_other_scopes(tmp_path, credential_store):
    import base64
    from backend.vault.store import _derive_key, _machine_passphrase, _xor_bytes

    def legacy(value, salt, scope):
        encrypted = _xor_bytes(value.encode(), _derive_key(_machine_passphrase(), salt))
        return {"value": base64.b64encode(encrypted).decode(), "salt": base64.b64encode(salt).decode(), "scope": scope}

    vault_path = tmp_path / "vault.json"
    vault_path.write_text(json.dumps({"version": 1, "entries": {
        "GLOBAL": legacy("global", b"a" * 16, "global"),
        "MCP": legacy("server", b"b" * 16, "mcp:other"),
    }}), encoding="utf-8")
    vault = EnvVault(vault_path)
    assert vault.inject_into_env("run_command") == {"GLOBAL": "global"}
    first = json.loads(vault_path.read_bytes())
    assert "value" not in first["entries"]["GLOBAL"]
    assert first["entries"]["MCP"]["value"]
    assert vault.list_names(prefix="MCP") == [{"name": "MCP", "description": "", "scope": "mcp:other", "credential_status": "stored"}]
    assert vault.inject_into_env("mcp:other") == {"GLOBAL": "global", "MCP": "server"}
    assert all("value" not in entry and "salt" not in entry for entry in json.loads(vault_path.read_bytes())["entries"].values())
