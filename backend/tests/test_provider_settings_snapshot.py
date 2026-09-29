from __future__ import annotations

import pytest

from backend import config_helpers
from backend.services.llm_adapter_factory import build_wire_adapter


@pytest.mark.parametrize("resolver", ["get_image_generation_settings", "get_llm_settings_payload", "load_llm_settings"])
def test_provider_projection_uses_one_config_revision_but_refreshes_next_call(monkeypatch, resolver):
    reads = []

    def read_settings():
        revision = len(reads) + 1
        reads.append(revision)
        return {"llm": {"provider": "custom", "custom": {
            "model": f"model-{revision}", "image_model": f"image-{revision}",
            "base_url": "https://example.invalid/v1", "wire_api": "chat",
        }}}

    monkeypatch.setattr(config_helpers, "_load_effective_settings_json", read_settings)
    resolve = getattr(config_helpers, resolver)
    first, second = resolve(), resolve()
    if resolver == "get_image_generation_settings":
        assert (first["model"], second["model"]) == ("image-1", "image-2")
    elif resolver == "get_llm_settings_payload":
        assert (first["active_model"], second["active_model"]) == ("model-1", "model-2")
        assert first["custom"]["image_model"] == "image-1"
    else:
        assert (first.model, second.model) == ("model-1", "model-2")
    assert reads == [1, 2]


def test_selected_model_metadata_controls_tool_mode():
    settings = {"llm": {"provider": "custom", "custom": {
        "api_key": "fixture", "base_url": "https://example.invalid/v1", "wire_api": "chat",
        "model": "code-model", "model_metadata": {
            "code-model": {"tool_mode": "code_mode_only"},
            "direct-model": {"tool_mode": "direct"},
        },
    }}}

    assert config_helpers.load_llm_settings(settings).tool_mode == "code_mode_only"
    settings["llm"]["custom"]["model"] = "direct-model"
    selected = config_helpers.load_llm_settings(settings)
    assert selected.tool_mode == "direct"
    settings["llm"]["custom"]["wire_api"] = "anthropic"
    adapter = build_wire_adapter(config_helpers.load_llm_settings(settings))
    assert adapter.configured_tool_mode() == "direct"
