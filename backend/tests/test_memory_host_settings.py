from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import config_helpers
from backend.api import routes_llm
from backend.config import AppConfig, LLMSettings, load_config
from backend.conversations.repository import ConversationRepository
from backend.feature_flags import FeatureFlags
from backend.memory.file_memory import FileMemory
from backend.memory.settings import MemorySettings, memory_policy_snapshot


@pytest.fixture
def host_settings(monkeypatch, tmp_path):
    home = tmp_path / "host-config"
    home.mkdir()
    user = home / "settings.json"
    user.write_text(json.dumps({"llm": {"provider": "custom", "custom": {"base_url": "https://unused.invalid/v1", "model": "fixture", "wire_api": "chat"}},
        "unrelated": {"preserve": True}}), encoding="utf-8")
    managed = tmp_path / "managed"
    managed.mkdir()
    monkeypatch.setattr(config_helpers, "STATE_ROOT", home)
    monkeypatch.setattr(config_helpers, "SETTINGS_FILE", user)
    monkeypatch.setattr(routes_llm, "SETTINGS_FILE", user)
    monkeypatch.setenv("MINICODE_SYSTEM_CONFIG_FILE", str(tmp_path / "absent-system.toml"))
    monkeypatch.setenv("MINICODE_MANAGED_SETTINGS_DIR", str(managed))
    monkeypatch.setenv("MINICODE_REQUIREMENTS_FILE", str(tmp_path / "absent-requirements.toml"))
    monkeypatch.delenv("MINICODE_PROFILE", raising=False)
    monkeypatch.delenv("MINICODE_FEATURE_MEMORIES", raising=False)
    monkeypatch.setattr(routes_llm._state, "bootstrap", None)
    monkeypatch.setattr("backend.memory.file_memory.DATA_ROOT", tmp_path / "state")
    monkeypatch.setattr("backend.memory.file_memory.MEMORY_DIR", tmp_path / "state" / "memory")
    hook = AsyncMock(return_value=None)
    monkeypatch.setattr(routes_llm, "run_config_change_hook", hook)
    app = FastAPI()
    app.include_router(routes_llm.router)
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, user=user, hook=hook, home=home)


def test_host_master_saves_three_fields_without_changing_threads_memories_or_instructions(host_settings, tmp_path, monkeypatch):
    monkeypatch.setattr("backend.memory.file_memory.MEMORY_DIR", tmp_path / "preserved-memory")
    repository = ConversationRepository(tmp_path / "threads")
    disabled = repository.create_conversation(memory_mode="disabled")
    polluted = repository.create_conversation(memory_mode="polluted", memory_polluted=True)
    handbook = FileMemory().memory_dir / "MEMORY.md"
    handbook.write_text("Keep existing memory", encoding="utf-8")
    instructions = host_settings.home / "INSTRUCTIONS.md"
    instructions.write_text("Keep authored instructions", encoding="utf-8")
    before = host_settings.client.get("/api/settings/memory").json()
    assert before["feature_enabled"] is False
    assert before["memories"] == {"generate_memories": True, "use_memories": True, "disable_on_external_context": False}
    response = host_settings.client.put("/api/settings/memory", json={"enabled": True})
    assert response.status_code == 200, response.text
    saved = json.loads(host_settings.user.read_text(encoding="utf-8"))
    assert saved["features"]["memories"] is True
    assert saved["memories"] == {"generate_memories": True, "use_memories": True}
    assert saved["unrelated"] == {"preserve": True}
    saved["feature_flags"] = {"plugins": False}
    host_settings.user.write_text(json.dumps(saved), encoding="utf-8")
    assert memory_policy_snapshot(load_config(cwd=None, resolve_credentials=False)) == {
        "enabled": True, "generate_memories": True, "use_memories": True, "disable_on_external_context": False}
    assert repository.get_conversation(disabled.id).memory_mode == "disabled"
    assert repository.get_conversation(polluted.id).memory_mode == "polluted"
    assert handbook.read_text(encoding="utf-8") == "Keep existing memory"
    assert instructions.read_text(encoding="utf-8") == "Keep authored instructions"
    assert host_settings.client.put("/api/settings/memory", json={"enabled": False}).status_code == 200
    assert handbook.read_text(encoding="utf-8") == "Keep existing memory"


def test_generation_default_applies_only_to_new_threads_and_external_policy_is_independent(host_settings, tmp_path):
    repository = ConversationRepository(tmp_path / "threads")
    old = repository.create_conversation(memory_mode="enabled")
    saved = json.loads(host_settings.user.read_text(encoding="utf-8"))
    saved["features"] = {"memories": True}
    saved["memories"] = {"generate_memories": False, "use_memories": True, "no_memories_if_mcp_or_web_search": True}
    host_settings.user.write_text(json.dumps(saved), encoding="utf-8")
    assert repository.create_conversation().memory_mode == "disabled"
    assert repository.create_conversation(memory_mode="enabled").memory_mode == "enabled"
    assert repository.get_conversation(old.id).memory_mode == "enabled"
    response = host_settings.client.put("/api/settings/memory", json={"disable_on_external_context": False})
    assert response.status_code == 200, response.text
    config = load_config(cwd=None, resolve_credentials=False)
    assert config.memories == MemorySettings(generate_memories=False, use_memories=True, disable_on_external_context=False)
    assert "no_memories_if_mcp_or_web_search" not in json.loads(host_settings.user.read_text(encoding="utf-8"))["memories"]


@pytest.mark.parametrize("feature,generate,use,external,mode", [
    (False, True, True, False, "enabled"), (True, False, True, False, "enabled"),
    (True, True, False, False, "enabled"), (True, True, True, False, "disabled"),
    (True, True, True, True, "enabled"), (False, True, True, True, "disabled"),
    (True, True, True, False, "polluted"),
])
def test_real_run_captures_host_policy_for_injection_tools_startup_and_external_results(tmp_path, monkeypatch, feature, generate, use, external, mode):
    from backend.agent.message import AgentEvent
    from backend.agent.query_engine import QueryEngine
    from backend.agent.runtime import default_runtime
    from backend.tests.test_agent_runner_done_fallback import _NoopLLM, _Session, _run_session_scenario

    monkeypatch.setattr("backend.memory.file_memory.MEMORY_DIR", tmp_path / "memory")
    monkeypatch.setattr("backend.memory.file_memory.DATA_ROOT", tmp_path / "state")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    memory = FileMemory.for_workspace(workspace)
    (memory.memory_dir / "memory_summary.md").write_text("v1\nExisting memory summary", encoding="utf-8")
    config = AppConfig(llm=LLMSettings(api_key="fixture", provider="openai", base_url="https://unused.invalid/v1", model="gpt-test"),
        feature_flags=FeatureFlags({"memories": feature}), memories=MemorySettings(generate, use, external))
    observations, startup, forgetting = [], [], []
    live_config = [config]
    next_config = replace(config, feature_flags=FeatureFlags({"memories": False}), memories=replace(config.memories, use_memories=False, disable_on_external_context=False))
    events = [AgentEvent.tool_call("search", "web_search", {"query": "fixture"}),
        AgentEvent.tool_result("search", "External fixture", status="success", result_kind="search"),
        AgentEvent.agent_message_completed("Done", source="model_final")]
    session = _Session(workspace, [], runner_events=events)
    if mode == "polluted":
        session.conversation_repo.mark_memory_polluted("conv_runnerdone", ["previous-source"])
    else:
        session.conversation_repo.update_memory_mode("conv_runnerdone", mode)
    session._model_runtime_for_conversation = lambda _owner: None
    original = session._runner

    async def runner(**kwargs):
        observations.append({"policy": dict(kwargs["metadata"]["memory_settings"]),
            "memory": kwargs["context_builder"]._build_memory_context(), "tools": kwargs["tool_registry"].list_tools()})
        if feature and generate and use and external:
            live_config[0] = next_config
        async for event in original(**kwargs):
            yield event

    session.query_engine = QueryEngine(runner=runner)
    monkeypatch.setattr("backend.ws.agent_runner.load_config", lambda **_kwargs: live_config[0])
    monkeypatch.setattr("backend.ws.agent_runner.get_llm_provider", lambda: "openai")
    monkeypatch.setattr("backend.ws.agent_runner.get_available_models", lambda _provider="openai": ["gpt-test"])
    monkeypatch.setattr("backend.ws.agent_runner._get_or_create_session_llm", lambda _session, **_kwargs: _NoopLLM())
    monkeypatch.setattr("backend.memory.generation.schedule_memory_startup", lambda **kwargs: startup.append(kwargs))
    monkeypatch.setattr("backend.memory.generation.schedule_memory_forgetting", lambda **kwargs: forgetting.append(kwargs))
    async def run_sequence():
        await session._run_agent_locked("Do the fixture", conversation_id="conv_runnerdone",
            metadata={"agent_runtime": default_runtime(), "assistant_message_id": "memory-host-assistant", "user_message_id": "memory-host-user"})
        if feature and generate and use and external:
            await session._run_agent_locked("Next captured configuration", conversation_id="conv_runnerdone",
                metadata={"agent_runtime": default_runtime(), "assistant_message_id": "memory-host-assistant-next", "user_message_id": "memory-host-user-next"})
    _run_session_scenario(session, run_sequence())
    assert observations[0]["policy"] == memory_policy_snapshot(config)
    assert ("Existing memory summary" in observations[0]["memory"]) is (feature and use)
    assert ("memory_read" in observations[0]["tools"]) is (feature and use)
    assert bool(startup) is feature
    result = session.conversation_repo.get_conversation("conv_runnerdone")
    assert result.memory_mode == ("polluted" if external else mode)
    assert result.memory_polluted is (external or mode == "polluted")
    assert bool(forgetting) is (feature and external)
    if feature and generate and use and external:
        assert observations[1]["policy"] == memory_policy_snapshot(next_config)
        assert observations[1]["memory"] == ""
        assert "memory_read" not in observations[1]["tools"]
    assert (memory.memory_dir / "memory_summary.md").read_text(encoding="utf-8") == "v1\nExisting memory summary"


def test_feature_environment_lock_rejects_writes_before_touching_config(host_settings, monkeypatch):
    monkeypatch.setenv("MINICODE_FEATURE_MEMORIES", "false")
    before = host_settings.user.read_bytes()
    response = host_settings.client.put("/api/settings/memory", json={"enabled": True})
    assert response.status_code == 409
    assert host_settings.user.read_bytes() == before
    host_settings.hook.assert_not_called()


def test_host_write_retires_only_the_edited_legacy_key_and_canonical_read_wins(host_settings):
    initial = json.loads(host_settings.user.read_text(encoding="utf-8"))
    initial["feature_flags"] = {"memories": False, "global_search": False}
    initial["features"] = {"plugins": True}
    host_settings.user.write_text(json.dumps(initial), encoding="utf-8")
    response = host_settings.client.put("/api/settings/memory", json={"enabled": True})
    assert response.status_code == 200, response.text
    assert response.json()["feature_enabled"] is True
    saved = json.loads(host_settings.user.read_text(encoding="utf-8"))
    assert saved["features"] == {"plugins": True, "memories": True}
    assert saved["feature_flags"] == {"global_search": False}
    config = load_config(cwd=None, resolve_credentials=False)
    assert config.feature_flags.enabled("memories") is True
    assert config.feature_flags.enabled("global_search") is False
    assert config.config_layer_stack.origins()["features.global_search"].source.kind == "user"
    response = host_settings.client.put("/api/settings/feature-flags", json={"flags": {"memories": False}})
    assert response.status_code == 200, response.text
    saved = json.loads(host_settings.user.read_text(encoding="utf-8"))
    assert saved["features"]["memories"] is False
    assert saved["feature_flags"] == {"global_search": False}


@pytest.mark.parametrize("higher_source", ["project", "policy"])
def test_higher_canonical_layer_beats_a_lower_legacy_flag_with_real_origin(host_settings, tmp_path, monkeypatch, higher_source):
    initial = json.loads(host_settings.user.read_text(encoding="utf-8"))
    initial["feature_flags"] = {"memories": False, "global_search": False}
    host_settings.user.write_text(json.dumps(initial), encoding="utf-8")
    workspace = tmp_path / "repo"
    (workspace / ".git").mkdir(parents=True)
    (workspace / ".minicode").mkdir()
    monkeypatch.setattr("backend.workspace.trust.is_workspace_trusted", lambda _path: True)
    if higher_source == "project":
        (workspace / ".minicode" / "config.toml").write_text("[features]\nmemories=true\nplugins=false\n", encoding="utf-8")
    else:
        (workspace / ".minicode" / "config.toml").write_text("[feature_flags]\nmemories=false\n", encoding="utf-8")
        managed = tmp_path / "managed" / "managed-settings.json"
        managed.write_text(json.dumps({"features": {"memories": True, "plugins": False}}), encoding="utf-8")
    config = load_config(cwd=workspace, resolve_credentials=False)
    assert config.feature_flags.enabled("memories") is True
    assert config.feature_flags.enabled("global_search") is False
    assert config.feature_flags.enabled("plugins") is False
    assert config.config_layer_stack.origins()["features.memories"].source.kind == higher_source
    assert config.config_layer_stack.origins()["features.global_search"].source.kind == "user"
    assert "feature_flags" not in config.config_layer_stack.effective_config()
    assert json.loads(host_settings.user.read_text(encoding="utf-8")) == initial


@pytest.mark.parametrize("base_key,dropin_key,expected", [("feature_flags", "features", True), ("features", "feature_flags", False)])
def test_each_managed_file_normalizes_before_dropin_precedence(host_settings, tmp_path, base_key, dropin_key, expected):
    managed = tmp_path / "managed"
    (managed / "managed-settings.json").write_text(json.dumps({base_key: {"memories": not expected}}), encoding="utf-8")
    dropins = managed / "managed-settings.d"
    dropins.mkdir()
    (dropins / "10-memory.json").write_text(json.dumps({dropin_key: {"memories": expected}}), encoding="utf-8")
    config = load_config(cwd=None, resolve_credentials=False)
    assert config.feature_flags.enabled("memories") is expected
    assert config.config_layer_stack.origins()["features.memories"].source.kind == "policy"
