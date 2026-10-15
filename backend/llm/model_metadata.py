"""Exact catalog metadata used by Source's running-step admission checks."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

from backend.llm.provider_contracts import ModelMetadataSource

_CATALOG = json.loads(Path(__file__).with_name("source-model-admission-metadata.json").read_text(encoding="utf-8"))
_SOURCE = ModelMetadataSource(_CATALOG["reference_commit"], _CATALOG["source_file"])


def source_model_metadata_source(model_id: str) -> ModelMetadataSource | None:
    return _SOURCE if model_id in _CATALOG["models"] else None


def source_guardian_model_metadata(model_id: str) -> dict:
    return deepcopy(_CATALOG["models"].get(model_id, {}))
