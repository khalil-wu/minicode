"""Shared ChromaDB client configuration."""

from __future__ import annotations

from pathlib import Path
from typing import Any

NOOP_PRODUCT_TELEMETRY_IMPL = "backend.chroma_telemetry.NoopProductTelemetryClient"


def create_chroma_persistent_client(chromadb: Any, data_dir: Path) -> Any:
    settings = build_chroma_settings(chromadb)
    if settings is None:
        return chromadb.PersistentClient(path=str(data_dir))

    try:
        return chromadb.PersistentClient(path=str(data_dir), settings=settings)
    except TypeError:
        return chromadb.PersistentClient(path=str(data_dir))


def build_chroma_settings(chromadb: Any) -> Any | None:
    config_module = getattr(chromadb, "config", None)
    settings_cls = getattr(config_module, "Settings", None)
    if settings_cls is None:
        return None

    settings_kwargs = {
        "anonymized_telemetry": False,
        "chroma_product_telemetry_impl": NOOP_PRODUCT_TELEMETRY_IMPL,
        "chroma_telemetry_impl": NOOP_PRODUCT_TELEMETRY_IMPL,
    }
    try:
        return settings_cls(**settings_kwargs)
    except Exception:
        return settings_cls(anonymized_telemetry=False)
