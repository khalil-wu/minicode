"""Environment-variable vault backed by the operating-system credential store."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any

import keyring
from keyring.errors import KeyringError, PasswordDeleteError

from backend.config import STATE_ROOT
from backend.atomic_io import atomic_write_text

logger = logging.getLogger(__name__)

VAULT_FILE = STATE_ROOT / ".minicode" / "vault.json"
_KDF_ITERATIONS = 100_000  # legacy v1 migration only


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
        if not self._path.exists():
            self._entries = {}
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            for name, raw in data.get("entries", {}).items():
                self._entries[name] = _VaultEntry(
                    description=raw.get("description", ""),
                    scope=raw.get("scope", "global"),
                    encrypted_value=str(raw.get("value") or ""),
                    salt=str(raw.get("salt") or ""),
                )
        except (json.JSONDecodeError, OSError, KeyError, TypeError) as exc:
            logger.warning("vault load failed: %s", exc)
            self._entries = {}

    def _save(self) -> None:
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
        try:
            keyring.set_password(self._service, name, value)
        except KeyringError as exc:
            raise RuntimeError(f"OS credential store rejected the secret: {exc}") from exc
        self._entries[name] = _VaultEntry(
            description=description,
            scope=scope,
        )
        self._save()

    def get(self, name: str) -> str | None:
        entry = self._entries.get(name)
        if entry is None:
            return None
        try:
            value = keyring.get_password(self._service, name)
            if value is not None:
                return value
            # One-time migration from the v1 PBKDF2/XOR file. Successful
            # migration immediately removes ciphertext from disk.
            if not entry.encrypted_value or not entry.salt:
                return None
            key = _derive_key(_machine_passphrase(), base64.b64decode(entry.salt))
            decrypted = _xor_bytes(base64.b64decode(entry.encrypted_value), key)
            value = decrypted.decode()
            keyring.set_password(self._service, name, value)
            entry.encrypted_value = ""
            entry.salt = ""
            self._save()
            return value
        except (KeyringError, ValueError, UnicodeDecodeError) as exc:
            logger.warning("vault decrypt failed for %s: %s", name, exc)
            return None

    def delete(self, name: str) -> bool:
        if name not in self._entries:
            return False
        try:
            keyring.delete_password(self._service, name)
        except PasswordDeleteError:
            pass
        except KeyringError as exc:
            raise RuntimeError(f"OS credential store could not delete the secret: {exc}") from exc
        del self._entries[name]
        self._save()
        return True

    def list_names(self) -> list[dict[str, str]]:
        return [
            {"name": name, "description": entry.description, "scope": entry.scope}
            for name, entry in self._entries.items()
        ]

    def inject_into_env(self, scope: str = "global") -> dict[str, str]:
        result: dict[str, str] = {}
        for name, entry in self._entries.items():
            if entry.scope in (scope, "global"):
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
