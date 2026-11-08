from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from backend.agent.context import ContextBuilder
from backend.agent.model_execution import ModelExecutionSnapshot
from backend.agent.state import AgentState
from backend.agent.tool_schema_derivation import effective_toolset_policy, derive_turn_tool_schema_state
from backend.agents.model_catalog import INVALIDATED_CATALOG, MODEL_CATALOG_KIND, SpawnModelCatalog, SpawnModelChoice
from backend.artifact.store import ArtifactStore
from backend.config import AppConfig, LLMSettings, TokenBudget
from backend.config_layers import ConfigLayer, ConfigLayerSource, ConfigLayerStack
from backend.feature_flags import FeatureFlags
from backend.llm.base import LLMMessage, ToolCallEvent
from backend.llm.openai_adapter import OpenAIAdapter
from backend.llm.provider_contracts import ModelDefinition
from backend.llm.responses_input import prepare_third_party_response_items
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext
from backend.tools.agent_tools import TaskTool
from backend.tools.code_execution import ToolExecTool, ToolWaitTool
from backend.tools.registry import ToolRegistry


def model(name="visible", **extra):
    return ModelDefinition(provider="custom", id=name, name=name, api="openai-completions", base_url="",
        reasoning=True, reasoning_effort_levels=("low", "medium", "high"), default_reasoning_effort="medium",
        extra={"description": "Fast and capable", "service_tiers": [{"id": "priority"}], **extra})


@pytest.fixture
def catalog_fixture(tmp_path):
    artifacts = ArtifactStore(storage_dir=tmp_path / "artifacts")
    registry = ToolRegistry()
    registry.register(TaskTool(artifact_store=artifacts, workspace_root=tmp_path))
    registry.register(ToolExecTool())
    registry.register(ToolWaitTool())
    models = [model(), model("hidden", show_in_picker=False), model("disabled", multi_agent_version="disabled")]
    runtime = SimpleNamespace(get_models=lambda _provider: tuple(models))
    yield SimpleNamespace(registry=registry, models=models, runtime=runtime, workspace=tmp_path)
    artifacts.shutdown()


def surface(fixture, *, enabled=True, version="v2", mode="code_mode_only", disabled=(), overrides=True, previous=None):
    config = AppConfig(llm=LLMSettings(provider="custom", model="visible", api_key="fixture"),
        feature_flags=FeatureFlags({"model_catalog_in_context": enabled, "multi_agent": True}),
        config_layer_stack=ConfigLayerStack((ConfigLayer(ConfigLayerSource(kind="user"), {
            "features": {"multi_agent_v2": {"expose_spawn_agent_model_overrides": overrides,
                "tool_namespace": "review_team", "hide_spawn_agent_metadata": False}},
        }),)))
    execution = ModelExecutionSnapshot(config=config, llm=None, provider="custom", model="visible", model_runtime=fixture.runtime)
    registry = fixture.registry.fork()
    permission = PermissionContext(mode="bypass", workspace_root=fixture.workspace)
    checker = PermissionChecker(config.permissions, fixture.workspace)
    policy = effective_toolset_policy(base_policy=None, tool_registry=registry, disabled_tools=set(disabled),
        requires_explicit_workspace=False, workspace_root=fixture.workspace, permission_mode="bypass",
        tool_mode=mode, model_execution=execution, multi_agent_version=version)
    schemas = registry.get_schemas(toolset_policy=policy, permission_checker=checker, permission_context=permission)
    state = derive_turn_tool_schema_state(base_tool_schemas=schemas, mcp_instructions={}, tool_registry=registry,
        toolset_policy=policy, permission_checker=checker, permission_context=permission, previous=previous)
    return schemas, state, policy


@pytest.mark.parametrize("version", ["v1", "v2"])
@pytest.mark.parametrize("enabled", [False, True])
def test_refresh_moves_catalog_without_rewriting_the_tool_contract(catalog_fixture, enabled, version):
    before, initial, policy = surface(catalog_fixture, enabled=enabled, version=version)
    listing = initial.model_catalog.fragment() if enabled else str(before)
    assert "- `visible`: Fast and capable" in listing
    assert "Reasoning efforts: low, medium (default), high." in listing
    assert "Service tiers: priority." in listing
    assert "hidden" not in listing
    assert ("- `disabled`:" in listing) == (version == "v1")
    assert ("<model_catalog> listing" in str(before)) == enabled
    assert ("Fast and capable" in str(before)) != enabled

    catalog_fixture.models[0] = model(description="Updated picker copy")
    after, updated, _ = surface(catalog_fixture, enabled=enabled, version=version, previous=initial)
    assert (before == after) == enabled
    assert "Updated picker copy" in (updated.model_catalog.fragment() if enabled else str(after))
    assert "Updated picker copy" not in (initial.model_catalog.fragment() if enabled else str(before))


@pytest.mark.parametrize("version,disabled,overrides", [
    ("disabled", (), True), ("v2", ("spawn_agent",), True), ("v2", (), False), ("v1", ("task",), True),
])
def test_catalog_requires_the_actual_available_override_tool(catalog_fixture, version, disabled, overrides):
    _, state, _ = surface(catalog_fixture, version=version, disabled=disabled, overrides=overrides)
    assert state.model_catalog is None


def test_catalog_byte_budget_sorts_selected_choices_and_keeps_identifiers_whole():
    choices = tuple(SpawnModelChoice(name, "说明<&\"'" * 60, ("low", "high"), "high", ("priority",))
        for name in ("zeta", "alpha", "omega", "beta", "delta"))
    catalog = SpawnModelCatalog(choices, "v2", "review_team", True, False)
    text = catalog.fragment()
    assert len(text.encode("utf-8")) <= 1000
    assert text.endswith("Additional model choices omitted.\n</model_catalog>")
    assert "`zeta`" not in text
    small = replace(catalog, choices=(SpawnModelChoice("a<&", "A<&\"'", ("low",), "low", ()),)).fragment()
    assert "a&lt;&amp;" in small and "&quot;&apos;" in small
    assert "Reasoning efforts: low (default)." in small


def test_filtering_precedes_the_five_choice_limit(catalog_fixture):
    catalog_fixture.models[:] = [model("hidden", visibility="hide"), model("unsupported", multi_agent_version="disabled"),
        *(model(f"choice-{index}") for index in range(7))]
    _, state, _ = surface(catalog_fixture)
    assert [choice.model for choice in state.model_catalog.choices] == [f"choice-{index}" for index in range(5)]


def test_append_only_catalog_survives_resume_and_refreshes_after_compaction(catalog_fixture):
    from backend.agent.context import clone_context_builder
    from backend.conversations.repository import ConversationRepository

    _, initial, policy = surface(catalog_fixture)
    builder = ContextBuilder(token_budget=TokenBudget(total=96000))
    state = AgentState(user_message="work")
    state.prompt_context.update(model_catalog=initial.model_catalog.fragment(), model_catalog_namespace="review_team")
    builder._sync_model_catalog(state)
    builder._history_store.append(LLMMessage(role="user", content="Original task", is_user_input=True))
    old = builder.export_snapshot()
    repository = ConversationRepository(catalog_fixture.workspace / "conversations")
    record = repository.create_conversation(context_snapshot=old)
    recovered = ConversationRepository(catalog_fixture.workspace / "conversations").get_execution_context(record.id)
    assert recovered.context_snapshot["history"] == old["history"]
    resumed = ContextBuilder(token_budget=TokenBudget(total=96000))
    resumed.load_snapshot(recovered.context_snapshot)
    resumed._sync_model_catalog(state)
    assert resumed.export_snapshot()["history"] == old["history"]
    catalog_fixture.models[0] = model(description="Refreshed")
    _, updated, _ = surface(catalog_fixture)
    state.prompt_context["model_catalog"] = updated.model_catalog.fragment()
    resumed._sync_model_catalog(state)
    assert resumed.export_snapshot()["history"][:2] == old["history"]
    assert len(resumed._history) == 3
    resumed._sync_model_catalog(state)
    assert len(resumed._history) == 3
    state.prompt_context["model_catalog"] = None
    resumed._sync_model_catalog(state)
    assert resumed._history[-1].content == INVALIDATED_CATALOG
    resumed._sync_model_catalog(state)
    assert len(resumed._history) == 4
    resumed._history = [LLMMessage(role="user", content="Compacted task")]
    state.prompt_context["model_catalog"] = updated.model_catalog.fragment()
    resumed._sync_model_catalog(state)
    assert len(resumed._history) == 2 and "Refreshed" in resumed._history[-1].content
    assert resumed._history[-1].content_kind == MODEL_CATALOG_KIND
    assert resumed._history[-1].content_metadata == {"provenance": {"type": "tool", "namespace": "review_team"}}
    fork = clone_context_builder(resumed)
    fork._history[-1].content_metadata["provenance"]["namespace"] = "child"
    assert resumed._history[-1].content_metadata["provenance"]["namespace"] == "review_team"


def test_picker_reordering_and_unselected_choices_do_not_append_context(catalog_fixture):
    catalog_fixture.models[:] = [model(f"model-{index}") for index in range(6)]
    before, initial, _ = surface(catalog_fixture)
    catalog_fixture.models[:5] = reversed(catalog_fixture.models[:5])
    after, reordered, _ = surface(catalog_fixture)
    assert initial.model_catalog.fragment() == reordered.model_catalog.fragment()
    assert before == after
    catalog_fixture.models[5] = model("model-5", description="outside bounded selection")
    _, unchanged, _ = surface(catalog_fixture)
    assert unchanged.model_catalog.fragment() == initial.model_catalog.fragment()


def test_captured_catalog_does_not_change_shared_model_or_policy_metadata(catalog_fixture):
    _, state, policy = surface(catalog_fixture)
    fragment = state.model_catalog.fragment()
    catalog_fixture.models[0].extra["description"] = "mutated live catalog"
    assert state.model_catalog.fragment() == fragment
    assert "model_catalog" not in str(policy.to_mapping())


@pytest.mark.asyncio
async def test_disabled_child_model_is_rejected_with_the_same_visible_choices(catalog_fixture):
    from backend.tests.test_model_execution_ownership import Model
    from backend.agent.run_context import RunContext
    from backend.tools.subagent_support import _resolve_subagent_llm

    definitions = catalog_fixture.models
    runtime = SimpleNamespace(get_models=lambda _provider: tuple(definitions),
        get_model=lambda _provider, name: next((m for m in definitions if m.id == name), None))
    settings = LLMSettings(provider="custom", model="visible", api_key="fixture")
    parent = Model(settings, None)
    execution = ModelExecutionSnapshot(config=AppConfig(llm=settings), llm=parent,
        provider="custom", model="visible", model_info=definitions[0], model_runtime=runtime)
    with pytest.raises(ValueError, match="Unknown model 'disabled'.*Available models: visible"):
        await _resolve_subagent_llm(parent, parent_metadata={}, run_context=RunContext(model_execution=execution),
            agent_type="default", model_override="disabled", multi_agent_version="v2", build_adapter=False)


def test_context_metadata_obeys_responses_feature_and_provider_boundaries():
    adapter = OpenAIAdapter(LLMSettings(provider="openai", model="fixture", api_key="fixture", wire_api="responses"))
    message = LLMMessage(role="developer", content="<model_catalog>\nExact choice\n</model_catalog>",
        content_kind=MODEL_CATALOG_KIND, content_metadata={"provenance": {"type": "tool", "namespace": "review_team"}})
    wire = adapter._build_responses_input([message])
    metadata = wire[0]["internal_chat_message_metadata_passthrough"]
    assert metadata["content_item_kinds"] == [MODEL_CATALOG_KIND]
    assert metadata["content_item_metadata"] == [message.content_metadata]
    assert "internal_chat_message_metadata_passthrough" not in prepare_third_party_response_items(wire)[0]
    adapter.configure_content_item_kinds(False)
    assert "internal_chat_message_metadata_passthrough" not in adapter._build_responses_input([message])[0]


@pytest.mark.asyncio
async def test_actual_query_refreshes_catalog_between_tool_result_and_next_request(tmp_path, monkeypatch):
    from backend.tests.test_model_execution_ownership import setup, execute

    async def behavior(current, messages):
        if len(current.inputs) == 1:
            definitions[0] = model("model-a", description="Changed between steps")
            return ToolCallEvent(id="inspect", name="inspect_model", arguments={})
        return None

    artifacts = ArtifactStore(storage_dir=tmp_path / "task-artifacts")
    task = TaskTool(artifact_store=artifacts, workspace_root=tmp_path)
    fixture = setup(tmp_path, monkeypatch, behavior, extra_tools=[task])
    definitions = [model("model-a", description="Initial choice")]
    fixture.owner.model_execution = replace(fixture.owner.model_execution,
        config=replace(fixture.owner.model_execution.config,
            feature_flags=FeatureFlags({"multi_agent": True, "model_catalog_in_context": True})))
    fixture.owner.model_execution.model_runtime.get_models = lambda _provider: tuple(definitions)
    tool_arrays = []
    original = fixture.model.stream_chat

    async def capture(messages, tools=None, metadata=None):
        tool_arrays.append(deepcopy(tools))
        async for event in original(messages, tools=tools, metadata=metadata):
            yield event

    monkeypatch.setattr(fixture.model, "stream_chat", capture)
    try:
        await execute(fixture, tmp_path)
    finally:
        artifacts.shutdown()
    assert len(fixture.model.inputs) == 2
    assert tool_arrays[0] == tool_arrays[1]
    catalogs = [[m.content for m in messages if m.content_kind == MODEL_CATALOG_KIND] for messages in fixture.model.inputs]
    assert len(catalogs[0]) == 1 and len(catalogs[1]) == 2
    assert catalogs[1][0] == catalogs[0][0]
    assert "Initial choice" in catalogs[0][0] and "Changed between steps" in catalogs[1][1]
