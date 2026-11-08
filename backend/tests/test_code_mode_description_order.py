from __future__ import annotations

import pytest
from functools import partial

from backend.config import AppConfig, LLMSettings
from backend.feature_flags import FeatureFlags
from backend.llm.provider_contracts import ModelDefinition
from backend.tests.test_code_execution import ScriptModel, run_model
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema
from backend.tools.contracts import ToolSpec


class NamespacedTool(BaseTool):
    name = "namespaced_lookup"
    description = "Aurora lookup tool instructions."
    permission = PermissionLevel.AUTO
    read_only = True

    def __init__(self, namespace_description):
        self.namespace_description = namespace_description
        self.calls = 0

    def get_schema(self):
        return ToolSchema(self.name, self.description, {"type": "object", "properties": {}},
            namespace="fixture-space", namespace_tool_name="lookup", namespace_description=self.namespace_description)

    def get_spec(self):
        return ToolSpec(self.name, exposure="deferred", toolset="core")

    async def execute(self, args, context=None):
        self.calls += 1
        return ToolResult("actual namespaced result")


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("namespace_description", ["Namespace instructions.", "  "])
async def test_actual_code_catalog_and_nested_search_use_the_captured_description_order(tmp_path, monkeypatch, enabled, namespace_description):
    monkeypatch.setattr("backend.tests.test_code_execution.AppConfig", partial(AppConfig,
        feature_flags=FeatureFlags({"code_mode_tool_description_first": enabled})))
    tool = NamespacedTool(namespace_description)
    code = """
        const name = 'fixture_space__lookup';
        text(ALL_TOOLS.find(tool => tool.name === name).description);
        const found = await tools.tool_search({query: 'Aurora lookup'});
        text(found.tools.find(tool => tool.name === name).description);
        const result = await tools[name]({});
        text(result.content);
    """
    model = ScriptModel(code)
    model._settings = LLMSettings(api_key="fixture", supports_search_tool=True)
    info = ModelDefinition(provider="custom", id="fixture", name="Search-capable fixture", api="openai-responses",
        base_url="", supports_search_tool=True)
    state, _, _, _, owner = await run_model(tmp_path, model, [tool], model_info=info)
    assert owner.model_execution.config.feature_flags.enabled("code_mode_tool_description_first") is enabled
    assert state.terminal_status == "completed", model.reports
    assert tool.calls == 1, model.reports
    expected = tool.description
    if namespace_description.strip():
        expected = (tool.description + "\n\n" + namespace_description if enabled
            else namespace_description + "\n\n" + tool.description)
    outputs = model.reports[-1]["output"]
    assert outputs[0] == expected
    assert outputs[1].startswith(expected + "\n\nDeclaration: tools.fixture_space__lookup(")
    assert outputs[2] == "actual namespaced result"
    # The setting controls code runtime definitions, not ordinary function descriptions.
    assert tool.name not in model.visible_tool_descriptions[0]
    assert tool.get_schema().to_openai_tool()["function"]["description"] == tool.description
