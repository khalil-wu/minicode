"""One current permission projection per synchronous provider preparation."""
from __future__ import annotations

from collections import Counter
from contextvars import ContextVar
from dataclasses import replace
from unittest.mock import Mock

import pytest

from backend.agent.tool_schema_derivation import derive_turn_tool_schema_state
from backend.agent.turn_iteration_runtime import TurnIterationRuntime
from backend.artifact.store import ArtifactStore
from backend.config import AppConfig, LLMSettings, PermissionSettings
from backend.config_layers import ConfigLayerStack
from backend.llm.base import ToolCallEvent
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext
from backend.services.tool_registry_factory import build_tool_registry
from backend.tools.base import BaseTool, ToolSchema
from backend.tools.code_execution import ToolExecTool
from backend.tools.registry import ToolRegistry
from backend.tools.toolsets import ToolsetPolicy


def prepare_schemas(registry, checker, permission, policy, *, previous=None):
    decisions = {}
    schemas = registry.get_schemas(permission_checker=checker, permission_context=permission,
        toolset_policy=policy, permission_decisions=decisions)
    return derive_turn_tool_schema_state(base_tool_schemas=schemas, mcp_instructions={},
        tool_registry=registry, permission_checker=checker, permission_context=permission,
        toolset_policy=policy, previous=previous, permission_decisions=decisions)


@pytest.mark.parametrize("code_mode_only", [False, True])
def test_real_tool_surface_shares_decisions_without_changing_provider_contract(tmp_path, monkeypatch, code_mode_only):
    config = AppConfig(llm=LLMSettings(api_key=""), config_layer_stack=ConfigLayerStack(()))
    artifacts = ArtifactStore(storage_dir=tmp_path / "artifacts")
    registry = build_tool_registry(artifacts, workspace_root=tmp_path, config=config, mcp_manager=None)
    checker = PermissionChecker(config.permissions, tmp_path)
    permission = checker.build_context(mode="confirm")
    policy = ToolsetPolicy(code_mode_only=code_mode_only, supports_search_tool=True)
    try:
        schemas = registry.get_schemas(permission_checker=checker, permission_context=permission, toolset_policy=policy)
        reference = derive_turn_tool_schema_state(base_tool_schemas=schemas, mcp_instructions={}, tool_registry=registry,
            permission_checker=checker, permission_context=permission, toolset_policy=policy)
        evaluate = Mock(wraps=checker.evaluate)
        monkeypatch.setattr(checker, "evaluate", evaluate)
        actual = prepare_schemas(registry.fork(), checker, permission, policy)
        counts = Counter(call.args[0] for call in evaluate.call_args_list)
        assert actual == reference
        assert len(counts) == len(registry.list_tools())
        assert set(counts.values()) == {1}
    finally:
        artifacts.shutdown()


def test_new_preparation_observes_owner_policy_and_replacement_generation(tmp_path):
    class OwnedTool(BaseTool):
        name = "owned_read"
        read_only = True

        def __init__(self, argument):
            self.argument = argument

        def get_schema(self):
            return ToolSchema(self.name, "Read owned data", {"type": "object",
                "properties": {self.argument: {"type": "string"}}, "required": [self.argument]})

        def is_capability_available(self, context=None):
            return context is not None and context.workspace_root == tmp_path / "owner-a"

        async def execute(self, args, context=None):
            raise AssertionError("Schema preparation does not execute tools")

    registry = ToolRegistry()
    registry.register(ToolExecTool())
    registry.register(OwnedTool("original"))
    checker = PermissionChecker(PermissionSettings(), tmp_path)
    permission = PermissionContext(mode="confirm", workspace_root=tmp_path / "owner-a")
    policy = ToolsetPolicy(code_mode_only=True)

    def description(state):
        return next(schema["function"]["description"] for schema in state.tool_schemas
            if schema["function"]["name"] == "tool_exec")

    first = prepare_schemas(registry, checker, permission, policy)
    assert "owned_read({ original: string" in description(first)
    other_owner = prepare_schemas(registry, checker, replace(permission, workspace_root=tmp_path / "owner-b"), policy, previous=first)
    assert "owned_read(" not in description(other_owner)
    checker._settings.always_deny.append("owned_read")
    denied = prepare_schemas(registry, checker, permission, policy, previous=other_owner)
    assert "owned_read(" not in description(denied)
    checker._settings.always_deny.clear()
    registry.register(OwnedTool("replacement"), replace=True)
    replacement = prepare_schemas(registry, checker, permission, policy, previous=denied)
    assert "owned_read({ replacement: string" in description(replacement)
    assert "owned_read({ original: string" in description(first)


@pytest.mark.asyncio
async def test_query_preparation_reuses_only_current_decisions_when_model_surface_changes(tmp_path, monkeypatch):
    from backend.tests.test_model_execution_ownership import execute, setup

    active_counts = ContextVar("schema_preparation_counts", default=None)
    preparations = []
    original_prepare = TurnIterationRuntime.prepare
    original_evaluate = PermissionChecker.evaluate

    async def counted_prepare(self, **kwargs):
        counts = Counter()
        token = active_counts.set(counts)
        try:
            result = await original_prepare(self, **kwargs)
            preparations.append(counts)
            return result
        finally:
            active_counts.reset(token)

    def counted_evaluate(self, name, *args, **kwargs):
        if (counts := active_counts.get()) is not None:
            counts[name] += 1
        return original_evaluate(self, name, *args, **kwargs)

    monkeypatch.setattr(TurnIterationRuntime, "prepare", counted_prepare)
    monkeypatch.setattr(PermissionChecker, "evaluate", counted_evaluate)
    fixture = None

    async def behavior(model, messages):
        if model.model_id() == "model-a":
            assert "inspect_model" in model.tool_names[-1] and "tool_exec" not in model.tool_names[-1]
            assert await fixture.runner.actions["set_model"]({"provider": "custom", "id": "model-b"})
            return ToolCallEvent(id="first-model-read", name="inspect_model", arguments={})
        assert "tool_exec" in model.tool_names[-1] and "inspect_model" not in model.tool_names[-1]
        return None

    fixture = setup(tmp_path, monkeypatch, behavior, tool_modes={"model-a": "direct", "model-b": "code_mode_only"})
    await execute(fixture, tmp_path)
    assert len(preparations) == 2
    assert [counts["inspect_model"] for counts in preparations] == [1, 1]
    assert all(max(counts.values()) == 1 for counts in preparations)
    assert fixture.inspector.observations == [("model-a", "model-a", "low", 96000)]
