"""Persistent MiniCode dynamic provider model catalog storage."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from backend.atomic_io import atomic_write_text, file_mutation_locks
from backend.config import STATE_ROOT


PROVIDER_MODELS_FILE = STATE_ROOT / ".minicode" / "models-store.json"
_MAX_MODELS_STORE_BYTES = 64 * 1024 * 1024


def _provider_id(value: Any) -> str:
    provider_id = str(value or "").strip()
    if not provider_id:
        raise ValueError("provider id must not be empty")
    return provider_id


def _clone_entry(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("models store entry must be an object")
    models = value.get("models")
    if not isinstance(models, list):
        raise ValueError("models store entry.models must be an array")
    legacy_fields = {"lastModified", "checkedAt"}.intersection(value)
    if legacy_fields:
        rendered = ", ".join(sorted(legacy_fields))
        raise ValueError(
            f"models store entry uses unsupported legacy fields: {rendered}"
        )
    for field_name in ("last_modified", "checked_at"):
        field_value = value.get(field_name)
        if field_name in value and (
            isinstance(field_value, bool)
            or not isinstance(field_value, (int, float))
        ):
            raise ValueError(f"models store entry.{field_name} must be a number")
    if "etag" in value and not isinstance(value.get("etag"), str):
        raise ValueError("models store entry.etag must be a string")
    try:
        serialized = json.dumps(
            dict(value),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("models store entry must contain finite JSON data") from exc
    if len(serialized.encode("utf-8")) > _MAX_MODELS_STORE_BYTES:
        raise ValueError("models store entry exceeds the 64 MiB limit")
    cloned = json.loads(serialized)
    return dict(cloned)


class ProviderModelsStorage:
    """Locked provider-scoped JSON model catalog store."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = Path(path or PROVIDER_MODELS_FILE)

    def _read_all_unlocked(self) -> dict[str, Any]:
        if not self._path.exists():
            return {}
        if self._path.stat().st_size > _MAX_MODELS_STORE_BYTES:
            raise ValueError("provider models store exceeds the 64 MiB limit")
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"provider models store is unreadable: {self._path}"
            ) from exc
        if not isinstance(payload, Mapping):
            raise ValueError("provider models store root must be an object")
        return dict(payload)

    def _read(self, provider_id: str) -> dict[str, Any] | None:
        with file_mutation_locks([self._path]):
            value = self._read_all_unlocked().get(provider_id)
            return _clone_entry(value) if value is not None else None

    async def read(self, provider_id: str) -> dict[str, Any] | None:
        clean_id = _provider_id(provider_id)
        return await asyncio.to_thread(self._read, clean_id)

    def _write(self, provider_id: str, entry: dict[str, Any]) -> None:
        with file_mutation_locks([self._path]):
            payload = self._read_all_unlocked()
            payload[provider_id] = entry
            rendered = json.dumps(
                payload,
                ensure_ascii=False,
                allow_nan=False,
                indent=2,
            )
            if len(rendered.encode("utf-8")) > _MAX_MODELS_STORE_BYTES:
                raise ValueError("provider models store exceeds the 64 MiB limit")
            atomic_write_text(self._path, rendered)

    async def write(self, provider_id: str, entry: Any) -> None:
        clean_id = _provider_id(provider_id)
        cloned = _clone_entry(entry)
        await asyncio.to_thread(self._write, clean_id, cloned)

    def _delete(self, provider_id: str) -> None:
        with file_mutation_locks([self._path]):
            payload = self._read_all_unlocked()
            if provider_id not in payload:
                return
            payload.pop(provider_id, None)
            atomic_write_text(
                self._path,
                json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2),
            )

    async def delete(self, provider_id: str) -> None:
        clean_id = _provider_id(provider_id)
        await asyncio.to_thread(self._delete, clean_id)


__all__ = ["PROVIDER_MODELS_FILE", "ProviderModelsStorage"]
