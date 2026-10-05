"""Environment-variable vault backed by the operating-system credential store."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
from typing import Any
from collections.abc import Callable, Mapping

import keyring
from keyring.errors import KeyringError, PasswordDeleteError

from backend.config import STATE_ROOT
from backend.atomic_io import atomic_write_text, file_mutation_locks

_CREDENTIAL_ERRORS = (KeyringError,)
if os.name == "nt":
    import pywintypes

    _CREDENTIAL_ERRORS += (pywintypes.error,)

VAULT_FILE = STATE_ROOT / ".minicode" / "vault.json"
_KDF_ITERATIONS = 100_000  # legacy v1 migration only


class VaultReadError(RuntimeError):
    """A persisted vault entry or index exists but cannot be read."""


def _derive_key(passphrase: bytes, salt: bytes, length: int = 32) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", passphrase, salt, _KDF_ITERATIONS, dklen=length)


def _xor_bytes(data: bytes, key: bytes) -> bytes:
    extended = (key * (len(data) // len(key) + 1))[: len(data)]
    return bytes(a ^ b for a, b in zip(data, extended))


def _machine_passphrase() -> bytes:
    node = os.environ.get("COMPUTERNAME", "") or os.environ.get("HOSTNAME", "")
    user = os.environ.get("USERNAME", "") or os.environ.get("USER", "")
    return f"minicode-vault-{node}-{user}".encode()


class EnvVault:
    """Manages encrypted environment variables stored locally."""

    def __init__(self, vault_path: Path | None = None) -> None:
        self._path = vault_path or VAULT_FILE
        self._entries: dict[str, _VaultEntry] = {}
        self._service = f"minicode:{hashlib.sha256(str(self._path.resolve()).encode()).hexdigest()[:20]}"
        self._load()

    def _load(self) -> None:
        with file_mutation_locks([self._path]):
            self._load_unlocked()

    def _load_unlocked(self) -> None:
        self._entries = {}
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("entries", {}), dict):
                raise ValueError("vault index must contain an entries object")
            for name, raw in data.get("entries", {}).items():
                if not isinstance(raw, dict):
                    raise ValueError("vault index entry must be an object")
                self._entries[name] = _VaultEntry(
                    description=raw.get("description", ""),
                    scope=raw.get("scope", "global"),
                    encrypted_value=str(raw.get("value") or ""),
                    salt=str(raw.get("salt") or ""),
                )
        except (OSError, KeyError, TypeError, ValueError) as exc:
            raise VaultReadError(f"Vault index at {self._path} is unreadable") from exc

    def _save_unlocked(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data: dict[str, Any] = {"version": 2, "backend": "os-keyring", "entries": {}}
        for name, entry in self._entries.items():
            data["entries"][name] = {
                "description": entry.description,
                "scope": entry.scope,
                # Preserve each legacy record until *that record* has been
                # successfully copied into the OS credential store. Saving a
                # different entry must not destroy still-unmigrated ciphertext.
                **({"value": entry.encrypted_value} if entry.encrypted_value else {}),
                **({"salt": entry.salt} if entry.salt else {}),
            }
        atomic_write_text(self._path, json.dumps(data, indent=2))

    def set(self, name: str, value: str, *, description: str = "", scope: str = "global") -> None:
        self.set_many({name: (value, description, scope)})

    def set_many(
        self, changes: Mapping[str, tuple[str | None, str, str]], *,
        publish: Callable[[], Any] | None = None,
    ) -> Any:
        """Commit one credential family; None removes a member of the family."""
        with file_mutation_locks([self._path]):
            self._load_unlocked()
            original_index = self._path.read_bytes() if self._path.exists() else None
            try:
                previous = {name: keyring.get_password(self._service, name) for name in changes}
            except _CREDENTIAL_ERRORS as exc:
                raise RuntimeError("OS credential store could not read the previous credential family") from exc
            try:
                for name, (value, description, scope) in changes.items():
                    if value is None:
                        try:
                            keyring.delete_password(self._service, name)
                        except PasswordDeleteError:
                            pass
                        self._entries.pop(name, None)
                    else:
                        keyring.set_password(self._service, name, value)
                        self._entries[name] = _VaultEntry(description=description, scope=scope)
                self._save_unlocked()
                return publish() if publish is not None else None
            except Exception as failure:
                rollback_errors = []
                for name, value in previous.items():
                    try:
                        if value is None:
                            try:
                                keyring.delete_password(self._service, name)
                            except PasswordDeleteError:
                                pass
                        else:
                            keyring.set_password(self._service, name, value)
                    except _CREDENTIAL_ERRORS as exc:
                        rollback_errors.append(exc)
                try:
                    if original_index is None:
                        if self._path.exists():
                            self._path.unlink()
                    else:
                        current_index = self._path.read_bytes() if self._path.exists() else None
                        if current_index != original_index:
                            atomic_write_text(self._path, original_index.decode("utf-8"))
                    self._load_unlocked()
                except (OSError, VaultReadError) as exc:
                    rollback_errors.append(exc)
                if rollback_errors:
                    raise ExceptionGroup("Credential family publication and rollback failed", [failure, *rollback_errors]) from failure
                if isinstance(failure, _CREDENTIAL_ERRORS):
                    raise RuntimeError("OS credential store rejected the credential family") from failure
                raise

    def get(self, name: str) -> str | None:
        with file_mutation_locks([self._path]):
            self._load_unlocked()
            entry = self._entries.get(name)
            if entry is None:
                return None
            return self._get_unlocked(name, entry)

    def _get_unlocked(self, name: str, entry: _VaultEntry) -> str | None:
        try:
            value = keyring.get_password(self._service, name)
            if value is not None:
                return value
            if not entry.encrypted_value and not entry.salt:
                return None
            # One-time migration from the v1 PBKDF2/XOR file. Successful
            # migration immediately removes ciphertext from disk.
            if not entry.encrypted_value or not entry.salt:
                raise VaultReadError(f"Vault entry {name} has incomplete legacy credential data")
            key = _derive_key(_machine_passphrase(), base64.b64decode(entry.salt, validate=True))
            decrypted = _xor_bytes(base64.b64decode(entry.encrypted_value, validate=True), key)
            value = decrypted.decode()
            keyring.set_password(self._service, name, value)
            entry.encrypted_value = ""
            entry.salt = ""
            self._save_unlocked()
            return value
        except (*_CREDENTIAL_ERRORS, ValueError, UnicodeDecodeError) as exc:
            raise VaultReadError(f"OS credential store could not read vault entry {name}") from exc

    def delete(self, name: str) -> bool:
        with file_mutation_locks([self._path]):
            self._load_unlocked()
            if name not in self._entries:
                return False
            self.set_many({name: (None, "", "")})
            return True

    def list_names(self) -> list[dict[str, str]]:
        with file_mutation_locks([self._path]):
            self._load_unlocked()
            return [
                {"name": name, "description": entry.description, "scope": entry.scope,
                 "credential_status": "stored" if self._get_unlocked(name, entry) is not None else "missing"}
                for name, entry in self._entries.items()
            ]

    def inject_into_env(self, scope: str = "global") -> dict[str, str]:
        # These records belong to Provider settings/auth, not user tool variables.
        provider_keys = {"OPENAI_API_KEY", "ANTHROPIC_API_KEY", "CUSTOM_API_KEY", "ANTHROPIC_AUTH_TOKEN"}
        provider_prefixes = (
            "OPENAI_API_KEY_", "ANTHROPIC_API_KEY_", "CUSTOM_API_KEY_",
            "MINICODE_OPENAI_IMAGE_API_KEY_", "MINICODE_ANTHROPIC_IMAGE_API_KEY_",
            "MINICODE_CUSTOM_IMAGE_API_KEY_", "MINICODE_PROVIDER_CREDENTIAL_",
        )
        with file_mutation_locks([self._path]):
            self._load_unlocked()
            names = [
                name for name, entry in self._entries.items()
                if entry.scope in (scope, "global")
                and name.upper() not in provider_keys
                and not name.upper().startswith(provider_prefixes)
            ]
        result: dict[str, str] = {}
        for name in names:
            value = self.get(name)
            if value is not None:
                result[name] = value
        return result


class _VaultEntry:
    __slots__ = ("encrypted_value", "salt", "description", "scope")

    def __init__(self, description: str = "", scope: str = "global", encrypted_value: str = "", salt: str = "") -> None:
        self.encrypted_value = encrypted_value
        self.salt = salt
        self.description = description
        self.scope = scope
