from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolate_all_runtime_data_dirs(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """One isolation owner for both test trees and every mutable runtime path."""
    conversations = tmp_path / "conversations"
    monkeypatch.setattr("backend.config.SETTINGS_FILE", tmp_path / "settings.json", raising=False)
    monkeypatch.setattr("backend.vault.store.VAULT_FILE", tmp_path / "vault.json", raising=False)
    monkeypatch.setattr("backend.conversations.repository.CONVERSATION_DATA_DIR", conversations, raising=False)
    monkeypatch.setattr("backend.ws.handler.CONVERSATION_DATA_DIR", conversations, raising=False)
    monkeypatch.setattr("backend.attachments.store.ATTACHMENT_DATA_DIR", tmp_path / "attachments", raising=False)
    monkeypatch.setattr("backend.artifact.store.ARTIFACT_DATA_DIR", tmp_path / "artifacts", raising=False)
    monkeypatch.setattr("backend.checkpoint.store.CHECKPOINT_DATA_DIR", tmp_path / "checkpoints", raising=False)
    monkeypatch.setattr(
        "backend.workspace.state.WORKSPACE_STATE_FILE",
        tmp_path / "active_workspace.json",
        raising=False,
    )
    # Production secrets use the OS credential store. Tests use the same
    # keyring API with an in-memory backend so they never touch the developer's
    # real Windows Credential Manager or depend on a desktop keyring in CI.
    keyring_values: dict[tuple[str, str], str] = {}

    def get_password(service: str, name: str) -> str | None:
        return keyring_values.get((service, name))

    def set_password(service: str, name: str, value: str) -> None:
        keyring_values[(service, name)] = value

    def delete_password(service: str, name: str) -> None:
        from keyring.errors import PasswordDeleteError

        if keyring_values.pop((service, name), None) is None:
            raise PasswordDeleteError("missing test credential")

    monkeypatch.setattr("keyring.get_password", get_password)
    monkeypatch.setattr("keyring.set_password", set_password)
    monkeypatch.setattr("keyring.delete_password", delete_password)

    from backend.workspace.state import clear_active_workspace_root

    clear_active_workspace_root()
    yield
    clear_active_workspace_root()
    for manager_path in ("backend.main._ws_manager", "backend.api._state.ws_manager"):
        module_name, attr = manager_path.rsplit(".", 1)
        try:
            module = __import__(module_name, fromlist=[attr])
            getattr(module, attr).reset_for_tests()
        except Exception:
            pass
