from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from backend.agent.context import ContextBuilder
from backend.agent.model_execution import ModelExecutionSnapshot
from backend.agent.run_context import RunContext
from backend.agent.runtime import AgentRuntime
from backend.agent.state import AgentState
from backend.agents.source_roles import (
    SourceAgentRole, inherit_role_restrictions, load_source_agent_roles,
    project_role_config, read_role_file, role_tool_denials, source_child_role,
)
from backend.artifact.store import ArtifactStore
from backend.config import AppConfig, LLMSettings, PermissionSettings
from backend.config_layers import ConfigLayer, ConfigLayerSource, ConfigLayerStack
from backend.feature_flags import FeatureFlags
from backend.llm.base import LLMAdapter, StreamEvent, StreamEventType
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.skills.loader import SkillLoader
from backend.skills.manager import SkillManager
from backend.tools.agent_tools import TaskTool
from backend.tools.command_tool import RunCommandTool
from backend.tools.file_tools import ReadFileTool
from backend.tools.registry import ToolRegistry
from backend.tools.subagent_catalog import agent_type_description, normalize_agent_type
from backend.tools.subagent_context import build_subagent_prompt
from backend.tools.subagent_runtime import route_child_approval_request
from backend.tools.subagent_support import _resolve_subagent_llm


def role_stack(folder: Path, declaration: dict, *, version: str = "v1") -> AppConfig:
    folder.mkdir(parents=True, exist_ok=True)
    return AppConfig(llm=LLMSettings(api_key="test", provider="custom", model="source-model"),
        feature_flags=FeatureFlags({"multi_agent_v2": version == "v2"}),
        config_layer_stack=ConfigLayerStack((ConfigLayer(ConfigLayerSource("user", file=str(folder / "config.toml")), {"agents": declaration}),)))


def test_layered_roles_discover_nested_toml_and_preserve_declared_precedence(tmp_path):
    user, project = tmp_path / "user", tmp_path / "project"
    (user / "agents").mkdir(parents=True)
    (project / "agents" / "nested").mkdir(parents=True)
    first = user / "agents" / "worker.toml"
    first.write_text('name="worker"\ndescription="User worker"\ndeveloper_instructions="Keep instructions"\nmodel="source-model"\n', encoding="utf-8")
    (project / "agents" / "nested" / "research.toml").write_text('name="researcher"\ndescription="Specific research"\ndeveloper_instructions="Research carefully"\n', encoding="utf-8")
    stack = ConfigLayerStack((
        ConfigLayer(ConfigLayerSource("user", file=str(user / "config.toml")), {"agents": {"worker": {"description": "Old metadata", "config_file": "agents/worker.toml"}}}),
        ConfigLayer(ConfigLayerSource("project", project_config_folder=str(project)), {"agents": {"worker": {"description": "Project guidance"}}}),
    ))
    roles = load_source_agent_roles(stack)
    assert roles["worker"].description == "Project guidance"
    assert roles["worker"].config_file == first.resolve()
    assert roles["worker"].overrides["developer_instructions"] == "Keep instructions"
    assert roles["researcher"].config_file == (project / "agents/nested/research.toml").resolve()


def test_malformed_discovered_roles_are_reported_and_not_selected(tmp_path, caplog):
    folder = tmp_path / "agents"
    folder.mkdir()
    (folder / "missing-instructions.toml").write_text('name="bad"\ndescription="Has no instructions"\n', encoding="utf-8")
    (folder / "bad-names.toml").write_text('name="bad-names"\ndescription="bad"\ndeveloper_instructions="x"\nnickname_candidates=["A", "A"]\n', encoding="utf-8")
    config = role_stack(tmp_path, {"enabled": True, "interrupt_message": True})
    assert load_source_agent_roles(config.config_layer_stack) == {}
    assert "Ignoring malformed agent role definition" in caplog.text


def test_role_only_projects_source_bounded_fields_and_relative_skill_paths(tmp_path):
    path = tmp_path / "bounded.toml"
    path.write_text('name="bounded"\ndescription="Bounded worker"\ndeveloper_instructions="Own one file"\n'
        'approval_policy="never"\nsandbox_mode="danger-full-access"\nmodel="source-model"\n'
        '[features]\nshell_tool=false\napps=false\nplugins=true\nmemory_tool=false\nrequest_permissions_tool=false\nweb_search=true\n'
        '[skills]\ninclude_instructions=false\nmax_context_tokens=999999\n'
        '[[skills.config]]\npath="skills/disabled"\nenabled=false\n'
        '[[skills.config]]\npath="skills/enabled"\nenabled=true\n', encoding="utf-8")
    role = read_role_file(path)
    assert role.overrides["features"] == {"shell_tool": False, "apps": False, "memories": False, "request_permissions_tool": False}
    assert role.overrides["skills"] == {"include_instructions": False, "config": [{"path": str((tmp_path / "skills/disabled").resolve()), "enabled": False}]}
    assert "approval_policy" not in role.overrides and "sandbox_mode" not in role.overrides


def test_source_explorer_is_not_a_private_read_only_prompt():
    assert normalize_agent_type(None) == "default"
    assert build_subagent_prompt("explorer", "Find the entry point") == "Find the entry point"
    assert build_subagent_prompt("worker", "Change one file") == "Change one file"
    assert "READ-ONLY" in build_subagent_prompt("explore", "Legacy investigation")
    role = SourceAgentRole("worker", "Configured worker", overrides={"model": "locked", "model_reasoning_effort": "high"})
    guidance = agent_type_description(["worker"], get_custom_agent=lambda name: role)
    assert "Configured worker" in guidance and "locked" in guidance and "cannot be changed" in guidance


@pytest.mark.parametrize("version, requested, expected", [("v1", False, False), ("v2", False, False), ("v2", True, True)])
def test_full_history_fork_role_layer_is_selected_only_by_source_rule(tmp_path, version, requested, expected):
    path = tmp_path / "worker.toml"
    path.write_text('developer_instructions="Worker instructions"\n', encoding="utf-8")
    config = role_stack(tmp_path, {"worker": {"description": "Worker", "config_file": "worker.toml"}}, version=version)
    result = source_child_role(config, role_name="worker", role_requested=requested, fork_turns="all")
    assert bool(result) is expected
    if version == "v1":
        with pytest.raises(ValueError, match="Full-history forked agents inherit"):
            source_child_role(config, role_name="worker", role_requested=True, fork_turns="all")


@pytest.mark.asyncio
async def test_role_model_is_applied_after_validating_request_and_preserves_prepared_effort(tmp_path, monkeypatch):
    from backend.tests.test_task_model_resolution import _parent_fixture, _parent_run_context

    parent, _runtime, config, metadata = _parent_fixture()
    path = tmp_path / "worker.toml"
    path.write_text('model="claude-opus-4-6"\n', encoding="utf-8")
    config.config_layer_stack = role_stack(tmp_path, {"worker": {"description": "Locked worker", "config_file": "worker.toml"}}).config_layer_stack
    metadata["_subagent_parent_runtime"]["config"] = config
    owner = _parent_run_context(metadata)
    result = await _resolve_subagent_llm(parent, parent_metadata={}, run_context=owner, agent_type="worker", model_override="claude-sonnet-4-6", effort_override="low", workspace_root=tmp_path, build_adapter=False)
    assert result.model == "claude-opus-4-6" and result.effort == "low"
    assert parent._settings.model == "claude-sonnet-4-6"
    with pytest.raises(ValueError, match="Unknown model"):
        await _resolve_subagent_llm(parent, parent_metadata={}, run_context=owner, agent_type="worker", model_override="invented-model", workspace_root=tmp_path, build_adapter=False)
    with pytest.raises(ValueError, match="not supported"):
        await _resolve_subagent_llm(parent, parent_metadata={}, run_context=owner, agent_type="worker", effort_override="ultra", workspace_root=tmp_path, build_adapter=False)


def test_role_projection_keeps_parent_authority_and_restrictions_for_cold_resume(tmp_path):
    config = role_stack(tmp_path, {})
    parent_permissions = config.permissions
    parent = project_role_config(config, {"developer_instructions": "Parent instructions", "features": {"shell_tool": False},
        "skills": {"config": [{"path": str(tmp_path / "parent-skill"), "enabled": False}]}})
    restrictions = inherit_role_restrictions(parent, {"developer_instructions": "Child instructions", "skills": {"config": [{"path": str(tmp_path / "child-skill"), "enabled": False}]}})
    resumed = project_role_config(config, restrictions)
    assert resumed.permissions is parent_permissions
    assert resumed.feature_flags.flags["shell_tool"] is False
    assert parent.config_layer_stack.effective_config()["developer_instructions"] == "Parent instructions"
    assert resumed.config_layer_stack.effective_config()["developer_instructions"] == "Child instructions"
    assert {entry["path"] for entry in restrictions["skills"]["config"]} == {str(tmp_path / "parent-skill"), str(tmp_path / "child-skill")}


def test_role_skills_filter_child_snapshot_and_refresh_without_changing_parent(tmp_path, monkeypatch):
    roots = []
    for level, name in [("user", "disabled"), ("user", "keep"), ("builtin", "bundled")]:
        directory = tmp_path / name
        directory.mkdir()
        (directory / "SKILL.md").write_text(f'---\nname: {name}\ndescription: {name} skill\n---\nInstructions\n', encoding="utf-8")
        roots.append((level, directory))
    monkeypatch.setattr(SkillLoader, "_search_dirs", lambda self: roots)
    manager = SkillManager(SkillLoader(tmp_path))
    manager.discover()
    child = manager.snapshot(tmp_path, role_config={"bundled": {"enabled": False}, "config": [{"path": str(tmp_path / "disabled"), "enabled": False}]})
    assert {meta.name for meta in child.list_metas()} == {"keep"}
    assert {meta.name for meta in manager.list_metas()} == {"disabled", "keep", "bundled"}
    assert {meta.name for meta in child.snapshot(tmp_path).list_metas()} == {"keep"}
    named = manager.snapshot(tmp_path, role_config={"config": [{"name": "keep", "enabled": False}]})
    assert {meta.name for meta in named.list_metas()} == {"disabled", "bundled"}
    builder = ContextBuilder(skill_manager=child, workspace_root=tmp_path, developer_instructions="Specific role instructions", include_skill_instructions=False)
    messages = builder._render_prompt_messages(AgentState(user_message="Hello"), tmp_path)
    assert messages[1].role == "developer" and messages[1].content == "Specific role instructions"
    assert all("keep skill" not in message.content for message in messages)


def test_model_surface_advertises_only_source_roles_and_keeps_explicit_legacy_schema(monkeypatch):
    from backend.agents.loader import AgentDefinition
    from backend.tools import agent_tools

    monkeypatch.setattr(agent_tools, "discover_agents", lambda root: {"custom-source": SourceAgentRole("custom-source", "Real source role"),
        "legacy-markdown": AgentDefinition("legacy-markdown", "Legacy user agent", "Legacy instructions")})
    tool = TaskTool(artifact_store=object())
    assert tool.model_schema().parameters["properties"]["agent_type"]["enum"] == ["default", "explorer", "worker", "custom-source"]
    assert {"general-purpose", "explore", "plan", "legacy-markdown"}.issubset(tool.get_schema().parameters["properties"]["agent_type"]["enum"])


def test_legacy_markdown_named_worker_does_not_replace_source_builtin(tmp_path, monkeypatch):
    from backend.agents import loader
    from backend.agents.source_roles import discover_execution_agents

    folder = tmp_path / "legacy-agents"
    folder.mkdir()
    (folder / "worker.md").write_text('---\nname: worker\ndescription: Old worker\n---\nOld instructions\n', encoding="utf-8")
    monkeypatch.setattr(loader, "_agent_search_dirs", lambda root=None: [folder])
    names = discover_execution_agents(tmp_path, config=role_stack(tmp_path, {}))
    assert "worker" not in names
    assert names["legacy:worker"].prompt == "Old instructions"
    assert build_subagent_prompt("worker", "Implement", get_custom_agent=names.get) == "Implement"
    assert "Old instructions" in build_subagent_prompt("legacy:worker", "Implement", get_custom_agent=names.get)


@pytest.mark.asyncio
async def test_cold_resume_reapplies_current_role_toml_instead_of_old_role_snapshot(tmp_path):
    from backend.tests.test_task_model_resolution import _parent_fixture, _parent_run_context

    parent, _runtime, config, metadata = _parent_fixture()
    path = tmp_path / "worker.toml"
    path.write_text('developer_instructions="Old role"\n[features]\nshell_tool=false\n', encoding="utf-8")
    config.config_layer_stack = role_stack(tmp_path, {"worker": {"description": "Worker", "config_file": "worker.toml"}}).config_layer_stack
    metadata["_subagent_parent_runtime"]["config"] = config
    owner = _parent_run_context(metadata)
    first = await _resolve_subagent_llm(parent, parent_metadata={}, run_context=owner, agent_type="worker", workspace_root=tmp_path, build_adapter=False)
    assert first.source_role_name == "worker"
    assert first.role_overrides["features"]["shell_tool"] is False
    path.write_text('developer_instructions="Current role"\n[features]\nshell_tool=true\n', encoding="utf-8")
    resumed = await _resolve_subagent_llm(parent, parent_metadata={}, run_context=owner, agent_type="worker", workspace_root=tmp_path,
        build_adapter=False, resume_source_role=True, resumed_role_name="worker")
    assert resumed.config.config_layer_stack.effective_config()["developer_instructions"] == "Current role"
    assert "shell_tool" not in resumed.role_overrides.get("features", {})


@pytest.mark.parametrize("subtype", ["request_permissions", "write_stdin"])
def test_child_approval_routes_parent_owner_without_reauthorizing_frozen_child(subtype):
    data = {"subtype": subtype, "tool_call_id": "call-1", "tool_name": "request_permissions" if subtype == "request_permissions" else "monitor",
        "args": {"permissions": {"network": {"enabled": True}}}, "permissions": {"network": {"enabled": True}},
        "stdin_action": {"id": "original-exec", "approval_id": "call-1", "input": "yes\n", "process_id": 77},
        "request_digest": "child-digest", "policy_identity": "child-policy", "turn_id": "child-turn", "message_id": "child-message", "conversation_id": "conversation"}
    parent = ToolExecutionContext(permission=PermissionContext(), conversation_id="conversation", metadata={"turn_id": "parent-turn", "assistant_message_id": "parent-message"})
    routed = route_child_approval_request(data, child_id="child", parent=parent)
    assert routed["tool_call_id"] == "child:call-1" and routed["turn_id"] == "parent-turn"
    assert routed["message_id"] == "parent-message" and routed["request_digest"] == "child-digest"
    assert routed["stdin_action"] == data["stdin_action"] and routed["permissions"] == data["permissions"]
    assert routed["child_approval_owner"]["turn_id"] == "child-turn"
    routed["permissions"]["network"]["enabled"] = False
    assert data["permissions"]["network"]["enabled"] is True and data["tool_call_id"] == "call-1"


class RecordingModel(LLMAdapter):
    def __init__(self):
        self.calls = []
        self._settings = LLMSettings(api_key="test", provider="custom", model="source-model")

    async def stream_chat(self, messages, tools=None, metadata=None):
        self.calls.append((messages, tools))
        yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="Finished")
        yield StreamEvent(type=StreamEventType.DONE)

    async def simple_chat(self, messages):
        return "Finished"

    def model_id(self):
        return "source-model"


@pytest.mark.asyncio
async def test_task_uses_source_role_developer_and_real_hidden_tool_surface(tmp_path):
    role = tmp_path / "worker.toml"
    role.write_text('developer_instructions="Own exactly feature.py"\n[features]\nshell_tool=false\nrequest_permissions_tool=false\n', encoding="utf-8")
    config = role_stack(tmp_path, {"worker": {"description": "Feature worker", "config_file": "worker.toml"}})
    config.agent = replace(config.agent, code_mode_only=False)
    model = RecordingModel()
    runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl", swarm_store_dir=tmp_path / "swarm", enable_lease_heartbeat=False)
    runtime.start_run(run_id="parent", conversation_id="conversation")
    registry = ToolRegistry()
    artifacts = ArtifactStore(storage_dir=tmp_path / "artifacts")
    registry.register(ReadFileTool(artifacts))
    registry.register(RunCommandTool(artifacts))
    checker = PermissionChecker(PermissionSettings(), workspace_root=tmp_path)
    task = TaskTool(artifact_store=artifacts, workspace_root=tmp_path)
    registry.register(task)
    snapshot = ModelExecutionSnapshot(config=config, llm=model, provider="custom", model="source-model")
    parent = ToolExecutionContext(permission=checker.build_context(mode="bypass"), workspace_root=tmp_path,
        conversation_id="conversation", session_id="session", metadata={"run_id": "parent", "turn_id": "parent-turn"},
        tool_registry=registry, permission_checker=checker, llm=model, model_execution=snapshot,
        run_context=RunContext(agent_runtime=runtime, model_execution=snapshot))
    try:
        result = await task.execute({"description": "Feature", "prompt": "Inspect feature.py", "agent_type": "worker"}, parent)
        assert not result.is_error, result.content
        assert model.calls
        messages, tools = model.calls[0]
        assert any(message.role == "developer" and message.content == "Own exactly feature.py" for message in messages)
        assert any(message.role == "user" and "Inspect feature.py" in message.content for message in messages)
        assert all("Task:\nInspect feature.py" not in message.content for message in messages)
        names = {tool["function"]["name"] for tool in tools}
        assert "read_file" in names and "run_command" not in names
        record = runtime.get_subagent(result.runtime_metadata["subagent_id"])
        assert record.agent_type == "worker"
        assert record.resume_config["source_role_overrides"]["features"]["shell_tool"] is False
        assert "shell_tool" not in config.feature_flags.flags
    finally:
        runtime.close(release_lease=True)
