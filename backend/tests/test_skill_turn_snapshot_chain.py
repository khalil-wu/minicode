"""One real turn shares discovery; the next turn refreshes physical skills."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.agent.context import ContextBuilder
from backend.agent.loop_session import AgentLoopSessionContext
from backend.agent.model_execution import ModelExecutionSnapshot
from backend.agent.query_engine import AgentSession, QueryEngine, QuerySubmission
from backend.agent.run_context import RunContext
from backend.agent.runtime import AgentRuntime
from backend.artifact.store import ArtifactStore
from backend.config import AppConfig, AgentSettings, LLMSettings, PermissionSettings, TokenBudget
from backend.config_layers import ConfigLayer, ConfigLayerSource, ConfigLayerStack
from backend.llm.base import LLMAdapter, StreamEvent, StreamEventType
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext
from backend.skills.loader import SkillLoader
from backend.skills.manager import SkillManager
from backend.tests.test_remote_mcp_skill_consumers import _catalog
from backend.tools.registry import ToolRegistry


def _skill(root, name, description):
    path = root / "skills" / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\ndescription: {description}\n---\nReal instructions.\n", encoding="utf-8")
    return path


def _host(root, monkeypatch, stack):
    monkeypatch.setattr(SkillLoader, "_search_dirs", lambda self: [("user", self._project_root / "skills")])
    loader = SkillLoader(root)
    loader._config_stack = stack
    host = SkillManager(loader)
    host.discover()
    calls = []
    discover = SkillLoader.discover

    def counted(loader):
        calls.append(loader._project_root)
        return discover(loader)

    monkeypatch.setattr(SkillLoader, "discover", counted)
    return host, calls


def test_captured_host_roles_and_executor_catalog_project_without_rediscovery(tmp_path, monkeypatch):
    root_a, root_b = tmp_path / "a", tmp_path / "b"
    _skill(root_a, "open", "Original")
    _skill(root_a, "private", "Restricted")
    _skill(root_b, "workspace-b", "Other workspace")
    stack = ConfigLayerStack(())
    host, scans = _host(root_a, monkeypatch, stack)
    captured = host.snapshot(config_stack=stack)
    projections = []
    project = SkillLoader.restrict_discovered_catalog

    def counted_projection(loader):
        projections.append(True)
        return project(loader)

    monkeypatch.setattr(SkillLoader, "restrict_discovered_catalog", counted_projection)
    catalog, _ = _catalog()
    captured.attach_executor_catalog(catalog)
    restricted = captured.snapshot(role_config={"config": [{"name": "private", "enabled": False}]}, config_stack=stack)
    projected = restricted.snapshot(role_config={}, config_stack=stack)
    assert [meta.name for meta in projected.list_metas()] == ["open"]
    assert captured.get_meta("private") is not None
    assert projected.executor_catalog is catalog
    assert len(scans) == 1
    assert len(projections) == 1
    repeated = projected.snapshot(role_config={"config": [{"name": "private", "enabled": False}]}, config_stack=stack)
    assert repeated.get_meta("private") is None
    assert len(projections) == 1
    _skill(root_a, "open", "Changed on disk")
    _skill(root_a, "installed", "Newly installed")
    assert projected.snapshot().get_meta("open").description == "Original"
    refreshed = projected.snapshot(force_reload=True)
    assert refreshed.get_meta("open").description == "Changed on disk"
    assert refreshed.get_meta("installed") is not None
    assert refreshed.get_meta("private") is None
    assert refreshed.executor_catalog is catalog
    changed_config = ConfigLayerStack((ConfigLayer(ConfigLayerSource("session_flags"),
        {"skills": {"config": [{"name": "open", "enabled": False}]}}),))
    assert projected.snapshot(config_stack=changed_config).get_meta("open") is None
    switched = projected.snapshot(root_b, config_stack=stack)
    assert [meta.name for meta in switched.list_metas()] == ["workspace-b"]
    assert switched.executor_catalog is catalog
    assert len(scans) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["captured_request", "sdk", "fork"])
async def test_query_and_bootstrap_share_one_scan_and_next_real_turn_sees_files(tmp_path, monkeypatch, entry):
    _skill(tmp_path, "owned", "Initial description")
    stack = ConfigLayerStack((ConfigLayer(ConfigLayerSource("user", file=str(tmp_path / "settings.json")), {}),))
    host, scans = _host(tmp_path, monkeypatch, stack)
    observed = []
    builder = ContextBuilder(workspace_root=tmp_path)

    class CatalogModel(LLMAdapter):
        async def stream_chat(self, messages, tools=None):
            observed.append({meta.name: meta.description for meta in builder.skill_manager.list_metas()})
            yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="Completed the requested work.", phase="final_answer")
            yield StreamEvent(type=StreamEventType.DONE, finish_reason="stop")

        async def simple_chat(self, messages):
            return ""

    model = CatalogModel()
    settings, budget = AgentSettings(max_iterations=2), TokenBudget()
    config = AppConfig(llm=LLMSettings(api_key="fixture"), agent=settings, token_budget=budget, config_layer_stack=stack)
    artifacts = ArtifactStore(storage_dir=tmp_path / "artifacts")
    runtime = AgentRuntime(metrics_file=tmp_path / "runtime/metrics.jsonl", enable_lease_heartbeat=False)
    session = AgentSession(llm=model, tool_registry=ToolRegistry(), artifact_store=artifacts,
        permission_checker=PermissionChecker(PermissionSettings(), tmp_path),
        agent_settings=settings, token_budget=budget, context_builder=builder)
    try:
        for index in range(3):
            source = (builder.skill_manager or host) if entry == "fork" else host
            run = RunContext(agent_runtime=runtime, is_subagent=entry == "fork",
                model_execution=ModelExecutionSnapshot.capture(config, model))
            if entry == "captured_request":
                source = host.snapshot(tmp_path, config_stack=stack)
                run.start_request = SimpleNamespace(skill_manager=source)
            events = [event async for event in QueryEngine().submit(QuerySubmission(
                user_message=f"Complete request {index}", session=session,
                runtime=AgentLoopSessionContext(skill_manager=source, workspace_root=tmp_path,
                    permission_context=PermissionContext(mode="bypass"), run_context=run,
                    metadata={"conversation_id": "skill-turn-owner"}))) ]
            assert any(event.type == "done" and event.data.get("status") == "completed" for event in events)
            assert len(scans) == index + 1
            if index == 0:
                _skill(tmp_path, "owned", "Edited description")
                _skill(tmp_path, "installed", "New skill")
            elif index == 1:
                (tmp_path / "skills/owned/SKILL.md").unlink()
    finally:
        await session.aclose()
        artifacts.shutdown()
        runtime.close(release_lease=True)
    assert observed == [
        {"owned": "Initial description"},
        {"installed": "New skill", "owned": "Edited description"},
        {"installed": "New skill"},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("fork_after_first", [False, True])
async def test_sdk_reused_run_context_and_real_environments_keep_current_turn_skills(tmp_path, monkeypatch, fork_after_first):
    from backend.sdk import create_session

    _skill(tmp_path, "owned", "Initial description")
    _skill(tmp_path, "private", "Role-restricted skill")
    stack = ConfigLayerStack((ConfigLayer(ConfigLayerSource("user", file=str(tmp_path / "settings.json")), {}),))
    host, scans = _host(tmp_path, monkeypatch, stack)
    builder = ContextBuilder(workspace_root=tmp_path, skill_manager=host, skill_config_stack=stack,
        skill_config={"config": [{"name": "private", "enabled": False}]})
    scans.clear()
    active = []
    observed = []

    class CatalogModel(LLMAdapter):
        async def stream_chat(self, messages, tools=None):
            manager = active[0].context_builder.skill_manager
            observed.append({meta.name: meta.description for meta in manager.list_metas()})
            assert manager.executor_catalog is not None
            yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="Completed the requested work.", phase="final_answer")
            yield StreamEvent(type=StreamEventType.DONE, finish_reason="stop")

        async def simple_chat(self, messages):
            return ""

    model = CatalogModel()
    config = AppConfig(llm=LLMSettings(api_key="fixture"), agent=AgentSettings(max_iterations=2), config_layer_stack=stack)
    artifacts = ArtifactStore(storage_dir=tmp_path / "artifacts")
    runtime = AgentRuntime(metrics_file=tmp_path / "runtime/metrics.jsonl", enable_lease_heartbeat=False)
    run = RunContext(agent_runtime=runtime)
    parent = create_session(session_id="persistent-skills", workspace_root=tmp_path, context_builder=builder,
        run_context=run, config=config, llm=model, artifact_store=artifacts, tool_registry=ToolRegistry(),
        permission_context=PermissionContext(mode="bypass"))
    session, child = parent, None
    try:
        for index in range(3):
            active[:] = [session]
            events = [event async for event in session.query(f"Finish request {index}")]
            assert any(event.type == "done" and event.data.get("status") == "completed" for event in events), [event.data for event in events if event.type == "error"]
            assert len(scans) == index + 1
            owner = session._query_kwargs["run_context"]
            assert owner.environment_manager is not None
            assert session._thread_environments is not None
            assert session.context_builder.skill_manager.get_meta("private") is None
            if index == 0:
                _skill(tmp_path, "owned", "Edited description")
                _skill(tmp_path, "installed", "New skill")
                if fork_after_first:
                    child = parent.fork(session_id="fork-skills")
                    session = child
            elif index == 1:
                (tmp_path / "skills/owned/SKILL.md").unlink()
        assert observed == [
            {"owned": "Initial description"},
            {"installed": "New skill", "owned": "Edited description"},
            {"installed": "New skill"},
        ]
        if child is not None:
            assert parent.context_builder.skill_manager.get_meta("owned").description == "Initial description"
    finally:
        if child is not None:
            await child.aclose()
        await parent.aclose()
        artifacts.shutdown()
        runtime.close(release_lease=True)
