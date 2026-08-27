"""MiniCode-owned provider credential storage."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import tempfile
import threading
import weakref
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from filelock import AsyncFileLock

from backend.vault import EnvVault


_AUTH_LOCK_ROOT = Path(tempfile.gettempdir()) / "minicode-provider-auth-locks"
_ASYNC_LOCKS_GUARD = threading.Lock()
_ASYNC_LOCKS: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Lock]]" = (
    weakref.WeakKeyDictionary()
)


def _credential_name(provider_id: str) -> str:
    digest = hashlib.sha256(provider_id.encode("utf-8")).hexdigest()[:24].upper()
    return f"MINICODE_PROVIDER_CREDENTIAL_{digest}"


class ProviderCredentialCorruptError(RuntimeError):
    """A stored provider credential exists but cannot be interpreted.

    This is deliberately distinct from "no credential stored": treating a
    corrupt blob as absent would silently present the provider as
    unauthenticated and hide the reason the login stopped working.
    """


class ProviderAuthStorage:
    """Store MiniCode provider credentials in the OS-backed vault."""

    def __init__(self, vault: EnvVault | None = None) -> None:
        self._vault = vault or EnvVault()

    def _mutation_lock_path(self, provider_id: str) -> Path:
        vault_path = getattr(self._vault, "_path", None)
        vault_identity = (
            str(Path(vault_path).resolve())
            if vault_path is not None
            else f"vault-object:{id(self._vault)}"
        )
        digest = hashlib.sha256(
            f"{vault_identity}\0{_credential_name(provider_id)}".encode(
                "utf-8", errors="replace"
            )
        ).hexdigest()
        return _AUTH_LOCK_ROOT / f"{digest}.lock"

    def _async_mutation_lock(self, provider_id: str) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        key = str(self._mutation_lock_path(provider_id))
        with _ASYNC_LOCKS_GUARD:
            locks = _ASYNC_LOCKS.setdefault(loop, {})
            lock = locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                locks[key] = lock
            return lock

    def get(self, provider_id: str) -> dict[str, Any] | None:
        raw = self._vault.get(_credential_name(provider_id))
        if not raw:
            return None
        try:
            value = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise ProviderCredentialCorruptError(
                f'Stored credential for provider "{provider_id}" is not readable JSON'
            ) from exc
        if not isinstance(value, Mapping):
            raise ProviderCredentialCorruptError(
                f'Stored credential for provider "{provider_id}" is not a credential object'
            )
        if value.get("type") not in {"oauth", "api_key"}:
            raise ProviderCredentialCorruptError(
                f'Stored credential for provider "{provider_id}" has an unsupported '
                f"credential type"
            )
        return dict(value)

    def set(self, provider_id: str, credentials: Mapping[str, Any]) -> None:
        payload = dict(credentials)
        if payload.get("type") not in {"oauth", "api_key"}:
            raise ValueError("Provider credential type must be 'oauth' or 'api_key'")
        self._vault.set(
            _credential_name(provider_id),
            json.dumps(payload, separators=(",", ":"), ensure_ascii=True),
            description=f"{provider_id} provider credentials",
            scope="global",
        )

    def delete(self, provider_id: str) -> bool:
        return self._vault.delete(_credential_name(provider_id))

    async def modify(self, provider_id: str, fn: Any) -> dict[str, Any] | None:
        """Run a serialized credential read-modify-write transaction.

        OAuth refresh must hold the provider lock across the network exchange:
        rotated refresh tokens are single-use for some providers, so a later
        compare-and-set is not sufficient.  The asyncio lock prevents another
        task in this process from relying on process-reentrant file-lock
        semantics; ``AsyncFileLock`` extends the same exclusion to other
        MiniCode processes using this vault.
        """

        if not callable(fn):
            raise TypeError("credential modifier must be callable")
        clean_id = str(provider_id or "").strip()
        if not clean_id:
            raise ValueError("provider id must not be empty")
        lock_path = self._mutation_lock_path(clean_id)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        async with self._async_mutation_lock(clean_id):
            process_lock = AsyncFileLock(
                str(lock_path),
                timeout=60,
                thread_local=False,
                run_in_executor=True,
            )
            async with process_lock:
                current = self.get(clean_id)
                next_value = fn(dict(current) if current is not None else None)
                if inspect.isawaitable(next_value):
                    next_value = await next_value
                if next_value is None:
                    return current
                if not isinstance(next_value, Mapping):
                    raise ValueError("credential modifier must return a credential object")
                payload = dict(next_value)
                self.set(clean_id, payload)
                return payload

    async def delete_serialized(self, provider_id: str) -> bool:
        """Delete one credential under the same lock used by ``modify``."""

        clean_id = str(provider_id or "").strip()
        if not clean_id:
            return False
        lock_path = self._mutation_lock_path(clean_id)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        async with self._async_mutation_lock(clean_id):
            process_lock = AsyncFileLock(
                str(lock_path),
                timeout=60,
                thread_local=False,
                run_in_executor=True,
            )
            async with process_lock:
                return self.delete(clean_id)


__all__ = ["ProviderAuthStorage"]
