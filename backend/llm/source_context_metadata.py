"""Exact model-owned context defaults packaged from the reference catalog."""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

from backend.agent.codex_prompts import codex_model_instructions

_CATALOG = json.loads(Path(__file__).with_name("source-context-model-capabilities.json").read_text(encoding="utf-8"))
_MODELS = _CATALOG["models"]
_FALLBACK = {**_CATALOG["fallback"], "model_messages": {"instructions_template": codex_model_instructions()}}


def source_context_metadata(model_id: str) -> dict:
    # models-manager/model_info.rs::model_info_from_slug owns unknown models:
    # local prompt, Bytes(10_000), and no original-image/context capability.
    return deepcopy(_MODELS.get(model_id, _FALLBACK))
