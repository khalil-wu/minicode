"""Receiving role/config/workspace scope survives executor capability refresh."""
from __future__ import annotations

import json
from pathlib import Path
import pytest

from backend.agent.context import ContextBuilder
from backend.agent.environment_runtime import refresh_run_environment_capabilities
from backend.agent.run_context import RunContext
from backend.config import AgentSettings, TokenBudget
from backend.config_layers import ConfigLayer, ConfigLayerSource, ConfigLayerStack
from backend.feature_flags import FeatureFlags
from backend.permissions.context import PermissionContext
from backend.skills.environment_catalog import ExecutorSkillCatalog
from backend.skills.loader import SkillLoader
from backend.skills.manager import SkillManager
from backend.tests.test_remote_mcp_skill_consumers import _catalog
from backend.tools.registry import ToolRegistry
from backend.tools.agent_tools import TaskTool
from backend.tools.skill_tools import ListSkillsTool, ReadSkillTool


def _skill(root: Path, name: str) -> Path:
    path = root / name / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text(f"---\nname: {name}\ndescription: {name} workflow\n---\n{name} instructions\n", encoding="utf-8")
    return path


def _stack(*rules) -> ConfigLayerStack:
    return ConfigLayerStack((ConfigLayer(ConfigLayerSource("session_flags"), {"skills": {"config": list(rules)}}),))


@pytest.mark.parametrize("restriction", ["path", "directory", "name", "bundled", "plugins"])
def test_captured_host_catalog_applies_receiving_role_without_rescan_or_parent_mutation(tmp_path, monkeypatch, restriction):
    path = _skill(tmp_path / "skills", "private-workflow")
    level = {"bundled": "builtin", "plugins": "plugin"}.get(restriction, "user")
    monkeypatch.setattr(SkillLoader, "_search_dirs", lambda _self: [(level, path.parent.parent)])
    loader = SkillLoader(tmp_path)
    loader._config_stack = _stack()
    host = SkillManager(loader)
    host.discover()
    assert host.load_skill_payload("private-workflow", path) is not None
    host.attach_executor_catalog(ExecutorSkillCatalog(()))

    if restriction == "bundled":
        role, features = {"bundled": {"enabled": False}}, {}
    elif restriction == "plugins":
        role, features = {}, {"plugins": False}
    else:
        selector = {"name": "private-workflow"} if restriction == "name" else {"path": str(path.parent if restriction == "directory" else path)}
        role, features = {"config": [{**selector, "enabled": False}]}, {}

    def unexpected_scan():
        raise AssertionError("Same captured host scope must not be rediscovered")

    monkeypatch.setattr(host._loader, "discover", unexpected_scan)
    context = ContextBuilder(skill_manager=host, workspace_root=tmp_path,
        skill_config=role, role_features=features, skill_config_stack=loader._config_stack)
    scoped = context.skill_manager
    assert scoped.executor_catalog is host.executor_catalog
    assert scoped.list_metas() == []
    assert scoped.load_skill_payload("private-workflow", path) is None
    assert scoped.readable_roots() == []
    assert scoped._loader._full_cache == {}
    assert "private-workflow" not in context._build_skill_catalog()
    assert host.load_skill_payload("private-workflow", path) is not None


def test_executor_bind_reapplies_config_and_preserves_authority_catalog(tmp_path, monkeypatch):
    path = _skill(tmp_path / "skills", "private-workflow")
    monkeypatch.setattr(SkillLoader, "_search_dirs", lambda _self: [("user", path.parent.parent)])
    loader = SkillLoader(tmp_path)
    loader._config_stack = _stack()
    host = SkillManager(loader)
    host.discover()
    catalog, _ = _catalog()
    host.attach_executor_catalog(catalog)
    context = ContextBuilder(skill_manager=host, workspace_root=tmp_path, skill_config_stack=loader._config_stack)
    assert context.skill_manager.load_skill_payload("private-workflow", path) is not None
    disabled = _stack({"name": "private-workflow", "enabled": False})
    context.bind_skill_manager(host, tmp_path, refresh=True, config_stack=disabled)
    assert context.skill_manager.load_skill_payload("private-workflow", path) is None
    assert context.skill_manager.executor_catalog is catalog
    assert host.load_skill_payload("private-workflow", path) is not None


def test_executor_bind_rebuilds_workspace_and_explicit_projectless_scope(tmp_path, monkeypatch):
    root_a, root_b = tmp_path / "a", tmp_path / "b"
    for root, name in ((root_a, "owned-a"), (root_b, "owned-b")):
        (root / ".git").mkdir(parents=True)
        _skill(root / ".minicode" / "skills", name)
    monkeypatch.setenv("MINICODE_CONFIG_DIR", str(tmp_path / "config"))
    loader = SkillLoader(root_a)
    loader._config_stack = _stack()
    host = SkillManager(loader)
    host.discover()
    host.attach_executor_catalog(ExecutorSkillCatalog(()))
    switched = host.snapshot(root_b)
    assert switched.get_meta("owned-b") is not None
    assert switched.get_meta("owned-a") is None
    assert host.snapshot(None).get_meta("owned-a") is None
    assert host.get_meta("owned-a") is not None
    assert host.executor_catalog is switched.executor_catalog


@pytest.mark.asyncio
@pytest.mark.parametrize("executor_enabled", [True, False])
async def test_host_role_scope_preserves_independent_executor_read_authority(tmp_path, monkeypatch, executor_enabled):
    private = _skill(tmp_path / "skills", "private-workflow")
    monkeypatch.setattr(SkillLoader, "_search_dirs", lambda _self: [("user", private.parent.parent)])
    loader = SkillLoader(tmp_path)
    loader._config_stack = _stack()
    host = SkillManager(loader)
    host.discover()
    catalog, document = _catalog(name="private-workflow", enabled=executor_enabled)
    host.attach_executor_catalog(catalog)
    context = ContextBuilder(skill_manager=host, workspace_root=tmp_path,
        skill_config={"config": [{"path": str(private), "enabled": False}]})
    assert context.skill_manager.load_skill_payload("private-workflow", private) is None
    # The host path rule is not a disablement for another executor's same-name
    # resource. Its captured enabled bit remains authoritative for list/read.
    scoped_catalog = context.skill_manager.executor_catalog
    listing = await ListSkillsTool(scoped_catalog).execute({"authority": {"kind": "executor"}})
    assert len(json.loads(listing.content)["skills"]) == int(executor_enabled)
    reading = await ReadSkillTool(scoped_catalog).execute({"package": catalog.entries[0].package})
    assert reading.is_error is not executor_enabled
    if executor_enabled:
        assert json.loads(reading.content)["contents"] == document
    else:
        assert "not available" in reading.content


class _EmptyTurnSnapshot:
    selected_capability_roots = ()

    def clone(self):
        return self

    def turn_environments(self):
        return ()

    def captured_environments(self):
        return {}

    def required_skills(self):
        return ()

    async def aclose(self):
        pass


@pytest.mark.asyncio
async def test_environment_refresh_scopes_parent_before_registry_and_keeps_same_turn_host_snapshot(tmp_path, monkeypatch):
    from backend.agent import environment_runtime

    private = _skill(tmp_path / "skills", "private-workflow")
    _skill(tmp_path / "skills", "open-workflow")
    monkeypatch.setattr(SkillLoader, "_search_dirs", lambda _self: [("user", private.parent.parent)])
    stack = _stack()
    loader = SkillLoader(tmp_path)
    loader._config_stack = stack
    host = SkillManager(loader)
    host.discover()
    role = {"config": [{"path": str(private), "enabled": False}]}
    parent_prepared = host.clone_host_catalog()
    parent_prepared.attach_executor_catalog(_catalog()[0])
    context = TaskTool._build_subagent_context_builder(context=None,
        token_budget=TokenBudget(), agent_settings=AgentSettings(), workspace_root=tmp_path,
        skill_manager=parent_prepared, skill_config=role, source_agent_role=True)
    context.configure_skill_stack(stack)
    assert context.skill_manager.load_skill_payload("private-workflow", private) is None
    run = RunContext(environment_manager=object(), environment_host_skill_manager=host, context_builder=context)
    registry = ToolRegistry()

    async def captured_environment(*_args, **_kwargs):
        return _EmptyTurnSnapshot()

    monkeypatch.setattr(environment_runtime, "prepare_run_environments", captured_environment)
    original_discover = SkillLoader.discover
    scans = []

    def counted_discover(self):
        scans.append(self._project_root)
        return original_discover(self)

    monkeypatch.setattr(SkillLoader, "discover", counted_discover)
    prepared = []
    try:
        for turn_id in ("child-turn", "child-turn", "child-next-turn"):
            current = await refresh_run_environment_capabilities(run,
                permission_context=PermissionContext(mode="bypass"), config_stack=stack,
                feature_flags=FeatureFlags({}), registry=registry, artifact_store=None,
                workspace_root=tmp_path, conversation_id="child-thread", turn_id=turn_id)
            prepared.append(current)
            context.bind_skill_manager(current.skill_manager, tmp_path, config_stack=stack)
            assert current.skill_manager.load_skill_payload("private-workflow", private) is None
            assert context.skill_manager.load_skill_payload("private-workflow", private) is None
            assert "private-workflow" not in context._build_skill_catalog()
            assert context.skill_manager.get_meta("open-workflow") is not None
            assert registry.get_tool("skills__read").catalog is current.skill_manager.executor_catalog
            assert registry.get_tool("skills__list").catalog is current.skill_manager.executor_catalog
            assert run.skill_manager is current.skill_manager
            assert len(scans) == (2 if turn_id == "child-next-turn" else 1)
        assert host.load_skill_payload("private-workflow", private) is not None
    finally:
        for task in run.environment_retirement_tasks:
            await task
        for current in prepared:
            await current.aclose()
