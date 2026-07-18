"""Encrypted environment variable vault.

Uses PBKDF2 key derivation + AES-like XOR stream cipher for local-at-rest
encryption. Not cryptographically hardened against targeted attacks, but
protects secrets from casual inspection of the vault file.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
from pathlib import Path
from typing import Any

from backend.config import STATE_ROOT

logger = logging.getLogger(__name__)

VAULT_FILE = STATE_ROOT / ".minicode" / "vault.json"
_KDF_ITERATIONS = 100_000
_SALT_BYTES = 16


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
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            self._entries = {}
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            for name, raw in data.get("entries", {}).items():
                self._entries[name] = _VaultEntry(
                    encrypted_value=raw["value"],
                    salt=raw["salt"],
                    description=raw.get("description", ""),
                    scope=raw.get("scope", "global"),
                )
        except (json.JSONDecodeError, OSError, KeyError) as exc:
            logger.warning("vault load failed: %s", exc)
            self._entries = {}

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data: dict[str, Any] = {"version": 1, "entries": {}}
        for name, entry in self._entries.items():
            data["entries"][name] = {
                "value": entry.encrypted_value,
                "salt": entry.salt,
                "description": entry.description,
                "scope": entry.scope,
            }
        self._path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def set(self, name: str, value: str, *, description: str = "", scope: str = "global") -> None:
        salt = base64.b64encode(secrets.token_bytes(_SALT_BYTES)).decode()
        key = _derive_key(_machine_passphrase(), base64.b64decode(salt))
        encrypted = base64.b64encode(_xor_bytes(value.encode(), key)).decode()
        self._entries[name] = _VaultEntry(
            encrypted_value=encrypted,
            salt=salt,
            description=description,
            scope=scope,
        )
        self._save()

    def get(self, name: str) -> str | None:
        entry = self._entries.get(name)
        if entry is None:
            return None
        try:
            key = _derive_key(_machine_passphrase(), base64.b64decode(entry.salt))
            decrypted = _xor_bytes(base64.b64decode(entry.encrypted_value), key)
            return decrypted.decode()
        except Exception as exc:
            logger.warning("vault decrypt failed for %s: %s", name, exc)
            return None

    def delete(self, name: str) -> bool:
        if name not in self._entries:
            return False
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

    def __init__(self, encrypted_value: str, salt: str, description: str = "", scope: str = "global") -> None:
        self.encrypted_value = encrypted_value
        self.salt = salt
        self.description = description
        self.scope = scope
