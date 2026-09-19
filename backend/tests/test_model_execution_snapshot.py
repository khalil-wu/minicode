from __future__ import annotations

from dataclasses import replace

import pytest

from backend.agent.model_execution import ModelExecutionSnapshot
from backend.config import AppConfig, PermissionSettings, TokenBudget
from backend.config_helpers import LLMSettings
from backend.config_layers import ConfigLayer, ConfigLayerSource, ConfigLayerStack
from backend.tests.test_model_execution_ownership import Model


@pytest.mark.parametrize("capture", [True, False])
def test_model_execution_snapshot_detaches_mutable_host_configuration(capture):
    settings = LLMSettings(
        api_key="fixture",
        provider="custom",
        model="captured",
        reasoning_effort="low",
    )
    config = AppConfig(
        llm=settings,
        token_budget=TokenBudget(total=64_000),
        permissions=PermissionSettings(auto_allow=["read_file"]),
        config_layer_stack=ConfigLayerStack((ConfigLayer(ConfigLayerSource(kind="user"), {"features": {"enabled": True}}),)),
    )
    model = Model(settings, lambda _model, _messages: None)

    snapshot = ModelExecutionSnapshot.capture(config, model) if capture else ModelExecutionSnapshot(config=config, llm=model, provider="custom", model="captured")
    initial_budget = config.token_budget
    config.permissions.auto_allow.append("later_tool")
    config.token_budget = replace(config.token_budget, total=8_192)
    config.llm = replace(config.llm, reasoning_effort="high")
    config.config_layer_stack.layers[0].config["features"]["enabled"] = False
    config.feature_flags.flags["sdk_query"] = False

    assert snapshot.config.permissions.auto_allow == ["read_file"]
    assert snapshot.config.token_budget.total == 64_000
    assert snapshot.config.llm.reasoning_effort == "low"
    assert snapshot.config.config_layer_stack.effective_config()["features"]["enabled"] is True
    assert snapshot.config.feature_flags.enabled("sdk_query")
    assert snapshot.config.token_budget is initial_budget
    assert snapshot.llm is model
