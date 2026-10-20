from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.config_layers import ConfigLayer, ConfigLayerSource, ConfigLayerStack
from backend.config_requirements import ConfigRequirements
from backend.feature_flags import FeatureFlags
from backend.hooks.discovery import discover_hook_snapshot
from backend.hooks.manager import HookManager
from backend.llm.model_request_policy import resolve_web_search_mode
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.permissions.approval_policy import ApprovalPolicy
from backend.services import agent_config_settings_service as config_service
from backend.services import hook_settings_service as hook_service
from backend.tools.web_tools import WebSearchTool
from backend.ws.permission_runtime import _managed_permission_projection


def stack(settings, path="settings.json"):
    return ConfigLayerStack((ConfigLayer(ConfigLayerSource("user", file=str(path)), settings),))


def test_hook_switch_changes_the_registry_used_at_turn_admission(monkeypatch, tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"hooks": {"pre_tool_use": [{"hooks": [{"type": "command", "command": "echo original"}]}]}}))

    def manager(_root):
        return HookManager.from_snapshot(discover_hook_snapshot(config_stack=stack(json.loads(path.read_text()), path),
            workspace_root=None, workspace_trusted=True, plugin_sources=[]))

    def update(mutator):
        settings = json.loads(path.read_text()); mutator(settings); path.write_text(json.dumps(settings))

    monkeypatch.setattr(hook_service, "load_hook_manager_for_workspace", manager)
    monkeypatch.setattr(hook_service, "_update_settings_json", update)
    monkeypatch.setattr(hook_service, "run_config_change_hook", AsyncMock(return_value=None))
    monkeypatch.setattr("backend.workspace.recent_projects.RecentProjectStore", lambda: SimpleNamespace(list=lambda: []))
    hook = hook_service.hook_settings()["hooks"][0]
    asyncio.run(hook_service.update_hook_settings([{"key": hook["key"], "enabled": False}]))
    assert manager(None).snapshot.executable_entries == ()
    assert hook_service.hook_settings()["hooks"][0]["enabled"] is False
    asyncio.run(hook_service.update_hook_settings([{"key": hook["key"], "enabled": True, "trusted_hash": hook["current_hash"]}]))
    assert len(manager(None).snapshot.executable_entries) == 1

    settings = json.loads(path.read_text())
    settings["hooks"]["pre_tool_use"][0]["hooks"][0]["command"] = "echo changed"
    path.write_text(json.dumps(settings))
    with pytest.raises(ValueError, match="changed since the trust request"):
        asyncio.run(hook_service.update_hook_settings([{"key": hook["key"], "trusted_hash": hook["current_hash"]}]))
    assert hook_service.hook_settings()["hooks"][0]["trust_status"] == "modified"


def test_config_controls_write_the_fields_consumed_by_permission_and_model_policy(monkeypatch):
    settings = {}
    def update(mutator): mutator(settings)
    monkeypatch.setattr(config_service, "_update_settings_json", update)
    monkeypatch.setattr(config_service, "load_config_layer_stack", lambda **_kwargs: stack(settings))
    monkeypatch.setattr(config_service, "run_config_change_hook", AsyncMock(return_value=None))
    result = asyncio.run(config_service.update_agent_config_settings({"approval_policy": "untrusted", "sandbox_mode": "read-only",
        "network_access": True, "model_verbosity": "high", "model_reasoning_summary": "detailed", "web_search": "cached"}))
    assert result["sandbox_mode"] == "read-only"
    assert settings["sandbox_workspace_write"]["network_access"] is True
    mode, policy, sandbox, _source = _managed_permission_projection("confirm", ApprovalPolicy.parse(settings["approval_policy"]), config_stack=stack(settings))
    assert (mode, policy, sandbox) == ("confirm", "untrusted", "read-only")
    assert _managed_permission_projection("bypass", config_stack=stack(settings))[2] == "danger-full-access"
    assert _managed_permission_projection("plan", config_stack=stack(settings))[2] == "read-only"


def test_explicit_host_sandbox_survives_projection_and_plan_stays_readonly():
    requirements = ConfigRequirements()
    assert requirements.sandbox_mode_for_permission_mode("confirm", sandbox_mode="read-only") == "read-only"
    assert requirements.sandbox_mode_for_permission_mode("bypass", sandbox_mode="external-sandbox") == "external-sandbox"
    assert requirements.sandbox_mode_for_permission_mode("plan", sandbox_mode="workspace-write") == "read-only"


@pytest.mark.parametrize("mode", ["disabled", "cached"])
def test_disabled_or_cached_search_never_calls_live_direct_source(monkeypatch, mode):
    config = SimpleNamespace(config_layer_stack=stack({"web_search": mode}), feature_flags=FeatureFlags({}))
    search = WebSearchTool()
    monkeypatch.setattr(search, "_direct_source_available", lambda: True)
    direct = AsyncMock(); monkeypatch.setattr(search, "_direct_search", direct)
    context = ToolExecutionContext(permission=PermissionContext(mode="confirm"), model_execution=SimpleNamespace(config=config))
    result = asyncio.run(search.execute({"query": "example"}, context))
    assert result.is_error
    direct.assert_not_awaited()


def test_cached_hosted_search_passes_no_external_web_access(monkeypatch):
    options = []
    async def query(_messages, **kwargs): options.append(kwargs["options"]); return "Cached search result"
    provider = SimpleNamespace(supports_hosted_web_search=lambda: True, hosted_web_search_supports_cached=lambda: True,
        hosted_web_search_supports_blocked_domains=lambda: False, side_query=query)
    search = WebSearchTool(provider); monkeypatch.setattr(search, "_direct_source_available", lambda: True)
    config = SimpleNamespace(config_layer_stack=stack({"web_search": "cached"}), feature_flags=FeatureFlags({}))
    result = asyncio.run(search.execute({"query": "example"}, ToolExecutionContext(permission=PermissionContext(mode="confirm"),
        llm=provider, model_execution=SimpleNamespace(config=config))))
    assert not result.is_error
    assert options[0].hosted_web_search is True
    assert options[0].web_search_external_web_access is False
    assert resolve_web_search_mode(config, permission_mode="bypass") == "live"


def test_cached_mode_reaches_the_actual_responses_request():
    from backend.llm.base import LLMMessage, SideQueryOptions
    from tests.test_openai_responses_alignment import _adapter
    from backend.tests.test_hosted_search_receipts import openai_response
    adapter, responses = _adapter([openai_response("completed")])
    asyncio.run(adapter.side_query([LLMMessage(role="user", content="Search archived reference")],
        options=SideQueryOptions(operation="web_search_tool", hosted_web_search=True, web_search_external_web_access=False)))
    assert responses.requests[0]["tools"][0]["external_web_access"] is False


def test_indexed_config_reaches_responses_without_calling_the_live_direct_source(monkeypatch):
    from backend.api.routes_llm import AgentConfigUpdate
    from tests.test_openai_responses_alignment import _adapter
    from backend.tests.test_hosted_search_receipts import openai_response

    settings = {}
    monkeypatch.setattr(config_service, "load_config_layer_stack", lambda **kwargs: stack(settings))
    monkeypatch.setattr(config_service, "_update_settings_json", lambda update: update(settings))
    monkeypatch.setattr(config_service, "run_config_change_hook", AsyncMock(return_value=None))
    request = AgentConfigUpdate(web_search="indexed")
    asyncio.run(config_service.update_agent_config_settings(request.model_dump(exclude_none=True, exclude={"workspace_root"})))
    config = SimpleNamespace(config_layer_stack=stack(settings), feature_flags=FeatureFlags({}))
    assert resolve_web_search_mode(config, permission_mode="bypass") == "indexed"
    adapter, responses = _adapter([openai_response("completed")])
    search = WebSearchTool(adapter)
    monkeypatch.setattr(search, "_direct_source_available", lambda: True)
    direct = AsyncMock()
    monkeypatch.setattr(search, "_direct_search", direct)
    result = asyncio.run(search.execute({"query": "indexed reference"}, ToolExecutionContext(
        permission=PermissionContext(mode="bypass"), llm=adapter, model_execution=SimpleNamespace(config=config))))
    assert not result.is_error
    direct.assert_not_awaited()
    assert responses.requests[0]["tools"] == [{"type": "web_search", "external_web_access": True, "indexed_web_access": True}]


def test_indexed_search_reports_a_provider_without_the_indexed_contract(monkeypatch):
    provider = SimpleNamespace(supports_hosted_web_search=lambda: True, hosted_web_search_supports_indexed=lambda: False)
    search = WebSearchTool(provider)
    direct = AsyncMock()
    monkeypatch.setattr(search, "_direct_source_available", lambda: True)
    monkeypatch.setattr(search, "_direct_search", direct)
    config = SimpleNamespace(config_layer_stack=stack({"web_search": "indexed"}), feature_flags=FeatureFlags({}))
    result = asyncio.run(search.execute({"query": "example"}, ToolExecutionContext(
        permission=PermissionContext(mode="confirm"), llm=provider, model_execution=SimpleNamespace(config=config))))
    assert result.is_error
    assert "Indexed web search is unavailable" in result.content
    direct.assert_not_awaited()
