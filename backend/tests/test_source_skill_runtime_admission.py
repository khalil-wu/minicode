"""Source originator/feature gates and pre-execution implicit Skill evidence."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.agent.context import ContextBuilder
from backend.agent.query_engine import AgentSession
from backend.agent.run_context import RunContext
from backend.agent.state import AgentState
from backend.artifact.store import ArtifactStore
from backend.config import AgentSettings, PermissionSettings, TokenBudget
from backend.feature_flags import FeatureFlags
from backend.llm.base import ToolCallEvent
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.sandbox.policy import NetworkSandboxPolicy, ResolvedSandboxPolicy, SandboxEnforcement
from backend.skills.implicit import admit_implicit_skill_invocation, detect_implicit_skill_invocation
from backend.skills.loader import SkillLoader
from backend.skills.manager import SkillManager
from backend.tests.test_code_execution import ScriptModel
from backend.tests.test_remote_mcp_skill_consumers import _catalog
from backend.tests.test_skill_mcp_dependency_policy import _skill_manager, _McpManager
from backend.agent.skill_activation import activate_turn_skills
from backend.tools.command_tool import RunCommandTool
from backend.tools.registry import ToolRegistry


def _owned_skill(tmp_path, monkeypatch):
    home = tmp_path / "minicode-home"
    skill = home / "skills" / "owned" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: owned\ndescription: User-owned real skill\n---\nUse the scripts.\n", encoding="utf-8")
    (skill.parent / "agents").mkdir()
    (skill.parent / "agents/openai.yaml").write_text("policy:\n  allow_implicit_invocation: false\n", encoding="utf-8")
    script = skill.parent / "scripts" / "actual.py"
    script.parent.mkdir()
    script.write_text("print('actual script ran')\nraise SystemExit(7)\n", encoding="utf-8")
    monkeypatch.setenv("MINICODE_CONFIG_DIR", str(home))
    loader = SkillLoader(tmp_path)
    monkeypatch.setattr(loader, "_search_dirs", lambda: [("user", home / "skills")])
    manager = SkillManager(loader)
    manager.discover()
    return manager, skill, script


@pytest.mark.asyncio
@pytest.mark.parametrize("originator,enabled", [("minicode", True), ("codex_cli", True), ("Codex fixture", False)])
async def test_source_dependency_gate_never_prompts_installs_or_admits_for_third_party_or_disabled_flag(tmp_path, monkeypatch, originator, enabled):
    manager = _McpManager()
    skills = _skill_manager(tmp_path, monkeypatch)
    state = AgentState(user_message="$read-docs", workspace_root=tmp_path)
    original = tmp_path / "mcp-config.json"
    original.write_text('{"servers":{}}', encoding="utf-8")
    before = original.read_bytes()

    async def forbidden(*args, **kwargs):
        raise AssertionError("Rejected dependency installation must not reach prompt/config writes")

    monkeypatch.setattr("backend.services.mcp_service.install_skill_mcp_servers", forbidden)
    _events = [event async for event in activate_turn_skills(skills, state.user_message, state, manager, forbidden, forbidden,
        permission_context=PermissionContext(approval_policy="never"),
        permission_profile=ResolvedSandboxPolicy(SandboxEnforcement.DISABLED, NetworkSandboxPolicy.ENABLED),
        feature_flags=FeatureFlags({"skill_mcp_dependency_install": enabled}), originator=originator)]
    assert manager.admitted == [] and not manager.connected and original.read_bytes() == before
    assert state.active_skills == ["read-docs"]


def test_implicit_runtime_only_evidence_deduplicates_current_enabled_catalog_without_implicit_prompt_filter(tmp_path, monkeypatch):
    skills, path, script = _owned_skill(tmp_path, monkeypatch)
    assert skills.list_metas()[0].allow_implicit_invocation is False
    builder = ContextBuilder(workspace_root=tmp_path)
    context = ToolExecutionContext(PermissionContext(mode="bypass"), workspace_root=tmp_path,
        run_context=RunContext(skill_manager=skills), turn_id="turn-a")
    args = {"command": f'python "{script}"'}
    first = admit_implicit_skill_invocation(tool_name="run_command", args=args, tool_context=context, context_builder=builder)
    second = admit_implicit_skill_invocation(tool_name="run_command", args=args, tool_context=context, context_builder=builder)
    assert first == second and first.source_kind == "host"
    assert len(skills._implicit_invocations) == 1
    assert builder.guardian_evidence.skills_for_turn("turn-a") == (str(path.resolve()),)
    assert builder.guardian_evidence.skills_for_turn("other-turn") == ()
    cold = ContextBuilder(workspace_root=tmp_path)
    cold.load_snapshot(builder.export_snapshot())
    assert cold.guardian_evidence.skills_for_turn("turn-a") == ()
    context.turn_id = "turn-b"
    admit_implicit_skill_invocation(tool_name="run_command", args=args, tool_context=context, context_builder=builder)
    assert builder.guardian_evidence.skills_for_turn("turn-a") == ()
    assert builder.guardian_evidence.skills_for_turn("turn-b") == (str(path.resolve()),)
    assert not skills.clone_host_catalog().executor_catalog


def test_remote_skill_and_lookalike_paths_cannot_become_host_owned_guardian_evidence(tmp_path, monkeypatch):
    skills, _path, _script = _owned_skill(tmp_path, monkeypatch)
    catalog, _ = _catalog()
    skills.attach_executor_catalog(catalog)
    invocation = detect_implicit_skill_invocation(skills, "python skills/exact/scripts/run.py", "file:///executor",
        environment_id="remote", platform_os="linux")
    assert invocation.source_kind == "executor" and invocation.resource.startswith("skill://selected/")
    assert detect_implicit_skill_invocation(skills, "python skills/exact/scripts/run.py", "file:///executor",
        environment_id="another-environment", platform_os="linux") is None
    builder = ContextBuilder(workspace_root=tmp_path)
    remote = SimpleNamespace(selection=SimpleNamespace(environment_id="remote", cwd="file:///executor"), peer_info={"platformOs": "linux"})
    context = ToolExecutionContext(PermissionContext(mode="bypass"), workspace_root=tmp_path,
        run_context=RunContext(skill_manager=skills), turn_id="remote-turn", environment_id="remote", turn_environment=remote)
    admitted = admit_implicit_skill_invocation(tool_name="run_command", args={"command": "cat skills/exact/SKILL.md"},
        tool_context=context, context_builder=builder)
    assert admitted.source_kind == "executor"
    assert builder.guardian_evidence.skills_for_turn("remote-turn") == ()


def test_context_binding_preserves_captured_executor_catalog_without_rescanning_host(tmp_path, monkeypatch):
    skills, _path, _script = _owned_skill(tmp_path, monkeypatch)
    catalog, _ = _catalog()
    skills.attach_executor_catalog(catalog)

    def unexpected_scan():
        raise AssertionError("An admitted turn catalog must not be replaced by host rediscovery")

    monkeypatch.setattr(skills._loader, "discover", unexpected_scan)
    context = ContextBuilder(workspace_root=tmp_path)
    context.bind_skill_manager(skills, tmp_path, refresh=True)
    assert context._skill_manager.executor_catalog is catalog
    assert context._skill_manager.list_metas()[0].name == "owned"


@pytest.mark.asyncio
async def test_real_failed_skill_command_was_admitted_before_execution_and_survives_only_current_turn(tmp_path, monkeypatch):
    from contextlib import aclosing
    from backend.agent.loop_session import AgentLoopSessionContext
    from backend.agent.query_engine import QueryEngine, QuerySubmission
    from backend.agent.runtime import AgentRuntime

    skills, path, script = _owned_skill(tmp_path, monkeypatch)
    command = f'python "{script}"'
    model = ScriptModel("", first_call=ToolCallEvent("actual-failed-command", "run_command", {"command": command}))
    registry = ToolRegistry()
    budget = TokenBudget(total=96000)
    builder = ContextBuilder(llm=model, token_budget=budget, workspace_root=tmp_path)
    artifacts = ArtifactStore(storage_dir=tmp_path / "artifacts")

    class ObservedCommand(RunCommandTool):
        async def execute(self, args, context=None):
            assert builder.guardian_evidence.skills_for_turn(context.turn_id) == (str(path.resolve()),)
            return await super().execute(args, context)

    registry.register(ObservedCommand(artifacts))
    session = AgentSession(llm=model, tool_registry=registry,
        artifact_store=artifacts, context_builder=builder,
        permission_checker=PermissionChecker(PermissionSettings(require_confirm=[], require_diff_review=[]), tmp_path),
        agent_settings=AgentSettings(max_iterations=3), token_budget=budget)
    runtime = AgentRuntime(metrics_file=tmp_path / "runtime/metrics.jsonl", swarm_store_dir=tmp_path / "runtime/swarm", enable_lease_heartbeat=False)
    state = AgentState(user_message="Run the real skill script", conversation_id="implicit-skill-owner", workspace_root=tmp_path)
    model.receipt_state = state
    run = RunContext(agent_runtime=runtime, skill_manager=skills)
    try:
        async with aclosing(QueryEngine().submit(QuerySubmission(user_message=state.user_message, session=session, state=state,
            runtime=AgentLoopSessionContext(session_id="implicit-session", workspace_root=tmp_path,
                permission_context=PermissionContext(mode="bypass"), skill_manager=skills, run_context=run)))) as stream:
            events = [event async for event in stream]
    finally:
        await session.aclose()
        artifacts.shutdown()
        runtime.close(release_lease=True)
    context = builder
    result = next(record for record in state.tool_calls if record.tool_call_id == "actual-failed-command")
    assert result.result_payload["exit_code"] == 7 and "actual script ran" in result.result_payload["summary"], result.result_payload
    actual_turn = context.latest_admitted_turn_id()
    assert actual_turn
    assert context.guardian_evidence.skills_for_turn(actual_turn) == (str(path.resolve()),)
    restored = ContextBuilder(workspace_root=tmp_path)
    restored.load_snapshot(context.export_snapshot())
    assert restored.guardian_evidence.skills_for_turn(actual_turn) == ()
