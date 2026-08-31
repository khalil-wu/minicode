from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.vault.store import EnvVault


def test_corrupt_index_refuses_mutations_instead_of_wiping(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault.json"
    vault_path.write_text("{ not json", encoding="utf-8")
    vault = EnvVault(vault_path)

    with pytest.raises(RuntimeError):
        vault.set("new-secret", "value")
    with pytest.raises(RuntimeError):
        vault.delete("anything")

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
