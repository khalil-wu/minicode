from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from backend.agent.state import AgentState
from backend.agent.tool_execution import toolset_policy_guard_reason
from backend.agent.tool_schema_derivation import effective_toolset_policy
from backend.llm.base import ToolCallEvent
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.tools.code_execution import ToolExecTool, ToolWaitTool
from backend.tools.registry import ToolRegistry
from backend.tools.tool_search import ToolSearchTool
from backend.tools.toolsets import ACTIVE_TOOLSET_POLICY_METADATA_KEY, ToolsetPolicy
from backend.tools.web_tools import WebSearchTool
from backend.vault.store import EnvVault, VaultReadError


def policy(registry, *, hosted=False, mode="direct"):
    return effective_toolset_policy(
        base_policy=ToolsetPolicy.default(), tool_registry=registry, disabled_tools=set(),
        requires_explicit_workspace=False, workspace_root=None, permission_mode="bypass",
        tool_mode=mode, hosted_web_search=hosted,
    )


@pytest.fixture
def search_case(tmp_path, monkeypatch):
    import backend.vault.store as vault_module

    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setattr(vault_module, "VAULT_FILE", tmp_path / "vault.json")
    search = WebSearchTool(lambda: pytest.fail("schema instantiated the parent model"))
    registry = ToolRegistry()
    registry.register(search)
    registry.register(ToolExecTool())
    registry.register(ToolWaitTool())
    registry.register(ToolSearchTool(registry))
    return search, registry


@pytest.mark.parametrize("mode", ["direct", "code_mode", "code_mode_only"])
def test_unconfigured_search_is_absent_from_schema_nested_directory_discovery_and_guard(search_case, mode):
    search, registry = search_case
    active = policy(registry, mode=mode)
    schemas = registry.get_schemas(toolset_policy=active)
    assert "web_search" not in {item["function"]["name"] for item in schemas}
    assert "tools.web_search(" not in json.dumps(schemas)
    view = registry.get_schema_view("web_search", toolset_policy=active)
    assert view.exposure == "hidden" and not view.code_mode_available
    context = ToolExecutionContext(
        permission=PermissionContext(mode="bypass"), tool_registry=registry,
        metadata={ACTIVE_TOOLSET_POLICY_METADATA_KEY: active, "_agent_state": AgentState(user_message="Research weather")},
    )
    found = asyncio.run(ToolSearchTool(registry).execute({"query": "select:web_search"}, context))
    assert json.loads(found.content)["matches"] == []
    reason = toolset_policy_guard_reason(
        ToolCallEvent(id="fabricated-search", name="web_search", arguments={"query": "weather"}), registry, context,
    )
    assert "web_search" in reason and reason
    assert registry.get_tool("web_search") is search

    available = policy(registry, hosted=True, mode=mode)
    view = registry.get_schema_view("web_search", toolset_policy=available)
    assert view.schema_available
    restored = registry.get_schemas(toolset_policy=available)
    if mode == "code_mode_only":
        assert "tools.web_search(" in json.dumps(restored)
    else:
        assert "web_search" in {item["function"]["name"] for item in restored}
    assert not active.is_available(registry.get_tool_spec("web_search"))


def test_tavily_presence_refreshes_after_real_vault_publication_without_repeated_secret_reads(search_case, monkeypatch):
    import backend.vault.store as vault_module

    search, registry = search_case
    stored = {}
    reads = []
    monkeypatch.setattr(vault_module.keyring, "get_password", lambda service, name: (reads.append(name), stored.get((service, name)))[1])
    monkeypatch.setattr(vault_module.keyring, "set_password", lambda service, name, value: stored.__setitem__((service, name), value))
    monkeypatch.setattr(vault_module.keyring, "delete_password", lambda service, name: stored.pop((service, name)))
    spec = registry.get_tool_spec("web_search")
    assert not policy(registry).is_available(spec)
    vault = EnvVault()
    vault.set("TAVILY_API_KEY", "fixture-search-key")
    reads.clear()
    assert policy(registry).is_available(spec)
    for _ in range(3):
        active = policy(registry)
        registry.get_schemas(toolset_policy=active)
        registry.build_schema_views(toolset_policy=active)
    assert reads == ["TAVILY_API_KEY"]
    assert search._direct_search_configured is True
    vault.delete("TAVILY_API_KEY")
    assert not policy(registry).is_available(spec)
    monkeypatch.setenv("TAVILY_API_KEY", "environment-search-key")
    assert policy(registry).is_available(spec)
    monkeypatch.delenv("TAVILY_API_KEY")
    assert not policy(registry).is_available(spec)


def test_unreadable_search_credential_is_an_explicit_configuration_error(search_case, monkeypatch):
    search, registry = search_case

    def unreadable():
        raise VaultReadError("Search credential store is unreadable")

    monkeypatch.setattr(search, "_search_api_key", unreadable)
    with pytest.raises(VaultReadError, match="Search credential store is unreadable"):
        policy(registry)
    assert policy(registry, hosted=True).is_available(registry.get_tool_spec("web_search"))
    monkeypatch.setattr(search, "_search_api_key", lambda: "repaired-key")
    assert policy(registry).is_available(registry.get_tool_spec("web_search"))


@pytest.mark.parametrize("wire,url,declared,expected", [
    ("responses", "https://api.openai.com/v1", None, True),
    ("responses", "https://gateway.invalid/v1", None, False),
    ("responses", "https://gateway.invalid/v1", True, True),
    ("chat", "https://api.openai.com/v1", True, False),
    ("anthropic", "https://api.anthropic.com", None, True),
])
def test_idle_catalog_uses_selected_wire_contract_without_instantiating_a_model(search_case, wire, url, declared, expected):
    from backend.ws.handler import WebSocketSession

    _, registry = search_case
    session = SimpleNamespace(
        run_manager=SimpleNamespace(context_for=lambda _: None), active_conversation_id="conversation",
        _extension_runtime_states={}, _model_runtime_for_conversation=lambda _: None,
        provider="custom", selected_model="selected",
        config=SimpleNamespace(llm=SimpleNamespace(tool_mode="direct"), agent=SimpleNamespace(code_mode_only=False)),
        session_lifecycle=SimpleNamespace(current_workspace_root=lambda: None),
        permission_context=PermissionContext(mode="bypass"),
        _llm_selection_payload=lambda: {"wire_api": wire, "base_url": url, "supports_hosted_web_search": declared},
    )
    active = WebSocketSession.runtime_toolset_policy(session, registry)
    assert active.is_available(registry.get_tool_spec("web_search")) is expected


@pytest.mark.asyncio
async def test_each_provider_step_uses_its_actual_model_and_restores_search_when_switching(search_case, tmp_path, monkeypatch):
    from backend.tests.test_model_execution_ownership import Model, execute, setup

    search, _ = search_case
    monkeypatch.setattr(Model, "supports_hosted_web_search", lambda self: self._settings.model == "model-b")
    fixture = None
    parent_snapshot = None
    parent_policy = None
    selections = []

    async def behavior(model, messages):
        nonlocal parent_policy
        selections.append(model._settings.model)
        active = fixture.owner.toolset_policy
        if len(selections) == 1:
            assert "web_search" not in model.tool_names[-1]
            parent_policy = active
            child = Model(replace(model._settings, model="model-b"), behavior)
            fixture.owner.model_execution = replace(
                parent_snapshot, model="model-b", llm=child,
                config=replace(fixture.config, llm=child._settings),
            )
            return ToolCallEvent(id="model-switch", name="inspect_model", arguments={})
        if len(selections) == 2:
            assert "web_search" in model.tool_names[-1]
            assert active.is_available(fixture.session.tool_registry.get_tool_spec("web_search"))
            assert not parent_policy.is_available(fixture.session.tool_registry.get_tool_spec("web_search"))
            fixture.owner.model_execution = parent_snapshot
            return ToolCallEvent(id="model-return", name="inspect_model", arguments={})
        assert "web_search" not in model.tool_names[-1]
        return None

    fixture = setup(tmp_path, monkeypatch, behavior, extra_tools=[search])
    parent_snapshot = fixture.owner.model_execution
    await execute(fixture, tmp_path)
    assert selections == ["model-a", "model-b", "model-a"]
    assert "web_search" not in fixture.owner.session_toolset_policy.disabled_tools
    assert fixture.session.tool_registry.get_tool("web_search") is search
