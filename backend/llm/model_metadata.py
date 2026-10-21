"""Exact catalog metadata used by Source's running-step admission checks."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

from backend.llm.provider_contracts import ModelMetadataSource

_CATALOG = json.loads(Path(__file__).with_name("source-model-admission-metadata.json").read_text(encoding="utf-8"))
_SOURCE = ModelMetadataSource(_CATALOG["reference_commit"], _CATALOG["source_file"])


def source_model_metadata_source(model_id: str) -> ModelMetadataSource | None:
    from backend.llm.bedrock import MODEL_ALIASES
    return _SOURCE if MODEL_ALIASES.get(model_id, model_id) in _CATALOG["models"] else None


def source_guardian_model_metadata(model_id: str) -> dict:
    from backend.llm.bedrock import MODEL_ALIASES
    value = deepcopy(_CATALOG["models"].get(MODEL_ALIASES.get(model_id, model_id), {}))
    if model_id in MODEL_ALIASES:
        value.update(use_responses_lite=False, tool_mode=None, web_search_tool_type="text",
            additional_speed_tiers=[], service_tiers=[], default_service_tier=None)
        if MODEL_ALIASES[model_id] == "gpt-5.5":
            value.update(context_window=272000, max_context_window=272000)
        if model_id.startswith(("global.", "us.")):
            value["supports_search_tool"] = False
        if model_id in {"openai.gpt-6-astra", "global.openai.gpt-6-astra", "us.openai.gpt-6-astra"}:
            value["service_tiers"] = [{"id": "ultrafast", "name": "Ultrafast",
                "description": "The fastest available responses for latency-sensitive work."}]
    return value
